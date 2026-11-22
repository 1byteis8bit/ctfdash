from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path
from typing import Any

from .config import ConfigError, InitCancelled, find_project, init_project, load_config
from .ctfd import CTFdError, sync_ctfd
from .events import EVENT_TYPES, Event, EventEngine, EventError, ingest_lines
from .store import Store, StoreError
from .ui import format_status, interactive_dashboard, load_payload
from .util import iso_now


def _project_parser() -> argparse.ArgumentParser:
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument(
        "--project",
        help="project directory or ctfdash.toml; otherwise search parents",
    )
    return parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ctfdash", description="Reusable CTFd terminal dashboard")
    subparsers = parser.add_subparsers(dest="command", required=True)
    init = subparsers.add_parser("init", help="run the interactive project setup wizard")
    init.add_argument("directory", nargs="?", default=".")

    parent = _project_parser()
    subparsers.add_parser("sync", parents=[parent], help="synchronize challenges and Top1-Top3")
    status = subparsers.add_parser("status", parents=[parent], help="print the current dashboard once")
    status.add_argument("--json", action="store_true", dest="as_json")
    dashboard = subparsers.add_parser("dashboard", parents=[parent], help="open the interactive dashboard")
    dashboard.add_argument("-i", "--interval", type=float)

    emit = subparsers.add_parser("emit", parents=[parent], help="record one runner event")
    emit.add_argument("event_type", choices=sorted(EVENT_TYPES))
    emit.add_argument("--challenge", type=int, required=True)
    emit.add_argument("--column", required=True)
    emit.add_argument("--at", default="now")
    emit.add_argument("--event-id", default=None)
    emit.add_argument("--index", type=int)
    emit.add_argument("--candidate")
    emit.add_argument("--value")
    emit.add_argument("--reason")
    emit.add_argument("--verdict", default="pending")
    emit.add_argument("--source", default="")
    emit.add_argument("--status", choices=("ok", "error"), default="ok")
    emit.add_argument("--stdin", action="store_true", help="read log content from stdin")

    ingest = subparsers.add_parser("ingest", parents=[parent], help="ingest newline-delimited JSON events")
    ingest.add_argument("--stdin", action="store_true", required=True)
    return parser


def _event_from_args(args: argparse.Namespace) -> Event:
    payload: dict[str, Any] = {}
    if args.index is not None:
        payload["index"] = args.index
    if args.event_type == "task.succeeded" and args.candidate:
        payload["candidate"] = args.candidate
    if args.event_type in ("task.failed", "task.excluded"):
        payload["reason"] = args.reason or "unspecified"
    if args.event_type == "candidate":
        payload.update(
            {
                "value": args.value or args.candidate or "",
                "verdict": args.verdict,
                "source": args.source,
            }
        )
    if args.event_type.startswith("log."):
        if not args.stdin:
            raise EventError("log events require --stdin")
        payload.update({"content": sys.stdin.read(), "status": args.status})
    return Event.from_dict(
        {
            "version": 1,
            "event_id": args.event_id or str(uuid.uuid4()),
            "type": args.event_type,
            "occurred_at": args.at,
            "challenge_id": args.challenge,
            "column_id": args.column,
            "payload": payload,
        }
    )


def _load(args: argparse.Namespace):
    return load_config(find_project(getattr(args, "project", None)))


def run(args: argparse.Namespace) -> int:
    if args.command == "init":
        path = init_project(Path(args.directory))
        config = load_config(path)
        with Store(config):
            pass
        print(f"초기 설정 완료: {path}")
        print("다음 단계: 인증 정보를 준비한 뒤 `ctfdash sync`를 실행하세요.")
        return 0

    config = _load(args)
    if args.command == "sync":
        with Store(config) as store:
            result = sync_ctfd(config, store)
        print(
            json.dumps(
                {
                    "ok": True,
                    "challenges": result.challenges,
                    "ranked_challenges": result.ranked_challenges,
                    "own_account_id": result.account_id,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "status":
        payload = load_payload(config)
        print(json.dumps(payload, ensure_ascii=False, indent=2) if args.as_json else format_status(config, payload))
        return 0 if payload.get("ok") else 1
    if args.command == "dashboard":
        if args.interval is not None and args.interval < 0.1:
            raise ConfigError("dashboard interval must be at least 0.1 seconds")
        return asyncio.run(interactive_dashboard(config, args.interval))
    if args.command == "emit":
        with Store(config) as store:
            result = EventEngine(config, store).apply(_event_from_args(args))
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False))
        return 0
    if args.command == "ingest":
        with Store(config) as store:
            result = ingest_lines(EventEngine(config, store), sys.stdin)
        print(json.dumps({"ok": True, **result}, ensure_ascii=False))
        return 0
    return 2


def main() -> None:
    try:
        code = run(build_parser().parse_args())
    except InitCancelled as exc:
        print(str(exc), file=sys.stderr)
        code = 130
    except (ConfigError, CTFdError, EventError, StoreError, RuntimeError) as exc:
        print(f"ctfdash: {exc}", file=sys.stderr)
        code = 1
    except KeyboardInterrupt:
        code = 130
    raise SystemExit(code)


if __name__ == "__main__":
    main()

