from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .config import Config
from .util import iso_now, json_dumps, parse_time


class StoreError(RuntimeError):
    pass


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS challenges (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  category TEXT NOT NULL DEFAULT '',
  value INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
  challenge_id INTEGER NOT NULL,
  column_id TEXT NOT NULL,
  status TEXT NOT NULL,
  queued_at TEXT,
  started_at TEXT,
  ended_at TEXT,
  solve_seconds REAL,
  failure_reason TEXT,
  session_index INTEGER NOT NULL DEFAULT 0,
  candidate TEXT,
  PRIMARY KEY(challenge_id, column_id),
  FOREIGN KEY(challenge_id) REFERENCES challenges(id)
);
CREATE TABLE IF NOT EXISTS events (
  event_id TEXT PRIMARY KEY,
  event_type TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  challenge_id INTEGER NOT NULL,
  column_id TEXT NOT NULL,
  payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS candidates (
  event_id TEXT PRIMARY KEY,
  challenge_id INTEGER NOT NULL,
  column_id TEXT NOT NULL,
  candidate TEXT NOT NULL,
  detected_at TEXT NOT NULL,
  verdict TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS first_solves (
  challenge_id INTEGER NOT NULL,
  rank INTEGER NOT NULL,
  account_id INTEGER,
  team_name TEXT NOT NULL,
  solved_at TEXT NOT NULL,
  elapsed_seconds REAL NOT NULL,
  fetched_at TEXT NOT NULL,
  PRIMARY KEY(challenge_id, rank)
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_candidates_unresolved ON candidates(verdict, detected_at);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(challenge_id, column_id, occurred_at);
"""


class Store:
    def __init__(self, config: Config):
        self.config = config
        config.state_dir.mkdir(parents=True, exist_ok=True)
        config.log_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(config.state_dir, 0o700)
        os.chmod(config.log_dir, 0o700)
        self.db = sqlite3.connect(config.database, timeout=5.0)
        os.chmod(config.database, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def set_meta(self, key: str, value: Any) -> None:
        self.db.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json_dumps(value)),
        )
        self.db.commit()

    def get_meta(self, key: str, default: Any = None) -> Any:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def replace_sync(
        self,
        challenges: list[dict[str, Any]],
        first_solves: dict[int, list[dict[str, Any]]],
        account_id: int | None,
    ) -> None:
        fetched_at = iso_now()
        valid_columns = tuple(column.id for column in self.config.columns)
        with self.db:
            for challenge in challenges:
                challenge_id = int(challenge["id"])
                category = str(challenge.get("category") or "")
                self.db.execute(
                    """INSERT INTO challenges(id,name,category,value,updated_at)
                       VALUES(?,?,?,?,?)
                       ON CONFLICT(id) DO UPDATE SET name=excluded.name,
                       category=excluded.category,value=excluded.value,updated_at=excluded.updated_at""",
                    (
                        challenge_id,
                        str(challenge.get("name") or challenge_id),
                        category,
                        int(challenge.get("value") or 0),
                        fetched_at,
                    ),
                )
                # Category exclusions affect only aggregate statistics. They must
                # remain runnable and visible; only explicit challenge IDs are
                # scheduling exclusions.
                excluded = challenge_id in self.config.excluded_challenge_ids
                for column_id in valid_columns:
                    status = "excluded" if excluded else "queued"
                    self.db.execute(
                        """INSERT OR IGNORE INTO tasks(
                           challenge_id,column_id,status,queued_at,failure_reason
                           ) VALUES(?,?,?,?,?)""",
                        (
                            challenge_id,
                            column_id,
                            status,
                            fetched_at,
                            "configured_exclusion" if excluded else None,
                        ),
                    )
                    if excluded:
                        self.db.execute(
                            """UPDATE tasks SET status='excluded',failure_reason='configured_exclusion'
                               WHERE challenge_id=? AND column_id=? AND status='queued'""",
                            (challenge_id, column_id),
                        )
                    else:
                        self.db.execute(
                            """UPDATE tasks SET status='queued',failure_reason=NULL
                               WHERE challenge_id=? AND column_id=? AND status='excluded'
                               AND failure_reason='configured_exclusion'""",
                            (challenge_id, column_id),
                        )
                self.db.execute("DELETE FROM first_solves WHERE challenge_id=?", (challenge_id,))
                for solve in first_solves.get(challenge_id, []):
                    self.db.execute(
                        """INSERT INTO first_solves(
                           challenge_id,rank,account_id,team_name,solved_at,elapsed_seconds,fetched_at
                           ) VALUES(?,?,?,?,?,?,?)""",
                        (
                            challenge_id,
                            int(solve["rank"]),
                            solve.get("account_id"),
                            str(solve["team_name"]),
                            str(solve["solved_at"]),
                            float(solve["elapsed_seconds"]),
                            fetched_at,
                        ),
                    )
            self.db.execute(
                "INSERT INTO meta(key,value) VALUES('last_sync_at',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (json_dumps(fetched_at),),
            )
            self.db.execute(
                "INSERT INTO meta(key,value) VALUES('own_account_id',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (json_dumps(account_id),),
            )

    def challenge_exists(self, challenge_id: int) -> bool:
        return self.db.execute("SELECT 1 FROM challenges WHERE id=?", (challenge_id,)).fetchone() is not None

    def task(self, challenge_id: int, column_id: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM tasks WHERE challenge_id=? AND column_id=?",
            (challenge_id, column_id),
        ).fetchone()

    def insert_event(
        self,
        event_id: str,
        event_type: str,
        occurred_at: str,
        challenge_id: int,
        column_id: str,
        payload: dict[str, Any],
    ) -> bool:
        cursor = self.db.execute(
            """INSERT OR IGNORE INTO events(
               event_id,event_type,occurred_at,challenge_id,column_id,payload_json
               ) VALUES(?,?,?,?,?,?)""",
            (event_id, event_type, occurred_at, challenge_id, column_id, json_dumps(payload)),
        )
        return bool(cursor.rowcount)

    def status_payload(self) -> dict[str, Any]:
        now = iso_now()
        challenges = [dict(row) for row in self.db.execute("SELECT * FROM challenges ORDER BY id")]
        tasks = [dict(row) for row in self.db.execute("SELECT * FROM tasks ORDER BY challenge_id,column_id")]
        top1 = {
            int(row["challenge_id"]): dict(row)
            for row in self.db.execute("SELECT * FROM first_solves WHERE rank=1")
        }
        unresolved = [
            dict(row)
            for row in self.db.execute(
                """SELECT ca.*,ch.name AS challenge FROM candidates ca
                   JOIN challenges ch ON ch.id=ca.challenge_id
                   WHERE ca.verdict='pending' OR ca.verdict LIKE 'submit_error%'
                   ORDER BY ca.detected_at DESC"""
            )
        ]
        newest: dict[tuple[int, str], dict[str, Any]] = {}
        for candidate in unresolved:
            newest.setdefault(
                (int(candidate["challenge_id"]), str(candidate["column_id"])), candidate
            )
        return {
            "ok": True,
            "now": now,
            "title": self.config.title,
            "contest_start": self.config.start.isoformat(),
            "contest_end": self.config.end.isoformat(),
            "columns": [column.__dict__ for column in self.config.columns],
            "excluded_categories": sorted(self.config.excluded_categories),
            "excluded_challenge_ids": sorted(self.config.excluded_challenge_ids),
            "last_sync_at": self.get_meta("last_sync_at"),
            "challenges": challenges,
            "tasks": tasks,
            "top1": top1,
            "submission_fallbacks": list(newest.values()),
        }


def ensure_started_before(started_at: str, ended_at: str) -> float:
    duration = (parse_time(ended_at) - parse_time(started_at)).total_seconds()
    if duration < 0:
        raise StoreError("event time is earlier than task start")
    return duration

