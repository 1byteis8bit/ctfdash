from __future__ import annotations

import builtins
import contextlib
import datetime as dt
import io
import json
import os
import stat
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from ctfdash.config import Column, Config, ConfigError, init_project, load_config
from ctfdash.ctfd import CTFdClient, CTFdError, sync_ctfd
from ctfdash.events import Event, EventEngine, EventError, ingest_lines
from ctfdash.store import Store
from ctfdash.ui import format_status

UTC = dt.timezone.utc


def make_config(root: Path, base_url: str = "https://ctf.example", session_file: Path | None = None) -> Config:
    return Config(
        root=root,
        title="Test CTF",
        base_url=base_url,
        start=dt.datetime(2026, 1, 1, tzinfo=UTC),
        end=dt.datetime(2026, 1, 2, tzinfo=UTC),
        timezone="UTC",
        flag_regex=r"flag\{[^{}]+\}",
        state_dir=root / ".ctfdash",
        token_env=None,
        session_file=session_file,
        columns=(Column("B", "Baseline", 0), Column("D2", "Fast", 1)),
        excluded_categories=frozenset({"osint"}),
        excluded_challenge_ids=frozenset({99}),
        refresh_seconds=2.0,
    )


class ConfigWizardTests(unittest.TestCase):
    def test_init_asks_required_values_and_creates_loadable_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            answers = iter(
                [
                    "Example Finals",
                    "https://play.example.test",
                    "",  # UTC
                    "2026-02-01 10:00",
                    "2026-02-01 18:00",
                    "",  # default flag regex
                    "",  # token auth
                    "",  # token environment
                    "B",
                    "Baseline",
                    "D2",
                    "Fast",
                    "",  # finish columns
                    "osint",
                    "10, 22",
                    "",  # refresh
                    "",  # confirm
                ]
            )
            fake_in = mock.Mock()
            fake_in.isatty.return_value = True
            fake_out = mock.Mock()
            fake_out.isatty.return_value = True
            with mock.patch.object(builtins, "input", side_effect=lambda _prompt="": next(answers)), \
                 mock.patch("sys.stdin", fake_in), mock.patch("sys.stdout", fake_out):
                path = init_project(Path(directory))
            config = load_config(path)
            self.assertEqual(config.title, "Example Finals")
            self.assertEqual([column.id for column in config.columns], ["B", "D2"])
            self.assertEqual(config.excluded_challenge_ids, frozenset({10, 22}))
            self.assertEqual(config.excluded_categories, frozenset({"osint"}))
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_init_refuses_non_interactive_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch("sys.stdin.isatty", return_value=False):
            with self.assertRaises(ConfigError):
                init_project(Path(directory))


class EventStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = make_config(self.root)
        self.store = Store(self.config)
        self.assertEqual(stat.S_IMODE(self.config.database.stat().st_mode), 0o600)
        self.store.replace_sync(
            [
                {"id": 1, "name": "Crypto One", "category": "crypto", "value": 100},
                {"id": 2, "name": "Find Me", "category": "osint", "value": 100},
            ],
            {
                1: [
                    {
                        "rank": 1,
                        "account_id": 7,
                        "team_name": "First",
                        "solved_at": "2026-01-01T00:02:00+00:00",
                        "elapsed_seconds": 120.0,
                    }
                ]
            },
            42,
        )
        self.engine = EventEngine(self.config, self.store)
        self.assertEqual(self.store.task(2, "D2")["status"], "queued")

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def event(self, event_type: str, event_id: str, at: str, **payload: object) -> Event:
        return Event.from_dict(
            {
                "event_id": event_id,
                "type": event_type,
                "occurred_at": at,
                "challenge_id": 1,
                "column_id": "D2",
                "payload": payload,
            }
        )

    def test_state_transitions_duration_reset_and_idempotency(self) -> None:
        started = self.event("task.started", "start", "2026-01-01T00:00:10+00:00")
        self.assertEqual(self.engine.apply(started), "applied")
        self.assertEqual(self.engine.apply(started), "duplicate")
        self.engine.apply(self.event("task.session", "session", "2026-01-01T00:00:20+00:00", index=2))
        self.engine.apply(
            self.event(
                "task.succeeded",
                "success",
                "2026-01-01T00:01:40+00:00",
                candidate="flag{real}",
            )
        )
        task = self.store.task(1, "D2")
        self.assertEqual(task["status"], "success")
        self.assertEqual(task["session_index"], 2)
        self.assertEqual(task["solve_seconds"], 90.0)
        with self.assertRaises(EventError):
            self.engine.apply(self.event("task.failed", "late-fail", "2026-01-01T00:02:00+00:00"))
        self.assertIsNone(
            self.store.db.execute("SELECT 1 FROM events WHERE event_id='late-fail'").fetchone()
        )
        self.engine.apply(self.event("task.queued", "reset", "2026-01-01T00:03:00+00:00"))
        self.assertEqual(self.store.task(1, "D2")["status"], "queued")

    def test_candidate_fallback_and_structured_log(self) -> None:
        self.engine.apply(
            self.event(
                "candidate",
                "candidate-1",
                "2026-01-01T00:01:00+00:00",
                value="flag{maybe}",
                verdict="submit_error:HTTPError",
                source="final_answer",
            )
        )
        self.engine.apply(
            self.event(
                "log.codex",
                "log-1",
                "2026-01-01T00:01:01+00:00",
                content="Found flag{maybe}",
                status="ok",
            )
        )
        payload = self.store.status_payload()
        self.assertEqual(payload["submission_fallbacks"][0]["candidate"], "flag{maybe}")
        log = self.config.log_dir / "1-D2.jsonl"
        record = json.loads(log.read_text(encoding="utf-8"))
        self.assertEqual(record["type"], "log.codex")
        self.assertEqual(record["content"], "Found flag{maybe}")

    def test_ingest_rejects_unknown_column_and_is_atomic_per_event(self) -> None:
        raw = json.dumps(
            {
                "event_id": "bad-column",
                "type": "task.started",
                "occurred_at": "2026-01-01T00:00:00+00:00",
                "challenge_id": 1,
                "column_id": "UNKNOWN",
            }
        )
        with self.assertRaises(EventError):
            ingest_lines(self.engine, [raw])
        self.assertIsNone(
            self.store.db.execute("SELECT 1 FROM events WHERE event_id='bad-column'").fetchone()
        )

    def test_dashboard_groups_categories_and_colors_delta(self) -> None:
        self.engine.apply(self.event("task.started", "start-ui", "2026-01-01T00:00:00+00:00"))
        self.engine.apply(self.event("task.succeeded", "done-ui", "2026-01-01T00:01:00+00:00"))
        rendered = format_status(self.config, self.store.status_payload(), use_color=False)
        self.assertIn("[ CRYPTO · 1문제 ]", rendered)
        self.assertIn("[ OSINT · 1문제 · 집계 제외 ]", rendered)
        self.assertIn("Fast 1/1 성공", rendered)
        self.assertIn("-1m 00.0s", rendered)


class FakeCTFdHandler(BaseHTTPRequestHandler):
    retry_count = 0

    def log_message(self, *_args: object) -> None:
        pass

    def send_payload(self, data: object, status: int = 200) -> None:
        body = json.dumps({"success": status == 200, "data": data}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.headers.get("Authorization") != "Token secret":
            self.send_payload({}, 403)
        elif self.path == "/api/v1/users/me":
            self.send_payload({"id": 42, "name": "ours"})
        elif self.path == "/api/v1/challenges":
            if FakeCTFdHandler.retry_count == 0:
                FakeCTFdHandler.retry_count += 1
                self.send_payload({}, 502)
            else:
                self.send_payload([{"id": 1, "name": "One", "category": "crypto", "value": 100}])
        elif self.path == "/api/v1/challenges/1":
            self.send_payload({"id": 1, "name": "One", "category": "crypto", "value": 100})
        elif self.path == "/api/v1/challenges/1/solves":
            self.send_payload(
                [
                    {"account_id": 42, "name": "ours", "date": "2026-01-01T00:00:30+00:00"},
                    {"account_id": 7, "name": "other", "date": "2026-01-01T00:01:00+00:00"},
                ]
            )
        else:
            self.send_payload({}, 404)


class CTFdSyncTests(unittest.TestCase):
    def test_sync_retries_once_and_excludes_own_account(self) -> None:
        FakeCTFdHandler.retry_count = 0
        server = ThreadingHTTPServer(("127.0.0.1", 0), FakeCTFdHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {"TEST_TOKEN": "secret"}):
                base = f"http://127.0.0.1:{server.server_port}"
                config = make_config(Path(directory), base)
                config = Config(**{**config.__dict__, "token_env": "TEST_TOKEN"})
                with Store(config) as store:
                    result = sync_ctfd(config, store)
                    rows = list(store.db.execute("SELECT * FROM first_solves"))
                self.assertEqual(result.challenges, 1)
                self.assertEqual(result.account_id, 42)
                self.assertEqual(rows[0]["team_name"], "other")
                self.assertEqual(rows[0]["elapsed_seconds"], 60.0)
                self.assertEqual(FakeCTFdHandler.retry_count, 1)
        finally:
            server.shutdown()
            server.server_close()

    def test_cookie_file_permissions_are_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session_file = root / "session.json"
            session_file.write_text('{"cookies": {"session": "x"}}', encoding="utf-8")
            session_file.chmod(0o644)
            with self.assertRaises(CTFdError):
                CTFdClient(make_config(root, session_file=session_file))


if __name__ == "__main__":
    unittest.main()

