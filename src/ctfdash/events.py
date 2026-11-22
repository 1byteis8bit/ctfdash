from __future__ import annotations

import fcntl
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .config import Config
from .store import Store, StoreError, ensure_started_before
from .util import json_dumps, normalize_time

TASK_EVENTS = {
    "task.queued",
    "task.started",
    "task.session",
    "task.succeeded",
    "task.failed",
    "task.excluded",
}
LOG_EVENTS = {"log.codex", "log.reasoning", "log.command", "log.result"}
EVENT_TYPES = TASK_EVENTS | LOG_EVENTS | {"candidate"}


class EventError(RuntimeError):
    pass


@dataclass(frozen=True)
class Event:
    version: int
    event_id: str
    type: str
    occurred_at: str
    challenge_id: int
    column_id: str
    payload: dict[str, Any]

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Event":
        try:
            event_type = str(raw["type"])
            if event_type not in EVENT_TYPES:
                raise EventError(f"unsupported event type: {event_type}")
            payload = raw.get("payload") or {}
            if not isinstance(payload, dict):
                raise EventError("payload must be an object")
            return cls(
                version=int(raw.get("version", 1)),
                event_id=str(raw.get("event_id") or uuid.uuid4()),
                type=event_type,
                occurred_at=normalize_time(str(raw.get("occurred_at") or "now")),
                challenge_id=int(raw["challenge_id"]),
                column_id=str(raw["column_id"]),
                payload=payload,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise EventError(f"invalid event: {exc}") from exc


class EventEngine:
    def __init__(self, config: Config, store: Store):
        self.config = config
        self.store = store
        self.column_ids = {column.id for column in config.columns}

    def apply(self, event: Event) -> str:
        if event.version != 1:
            raise EventError(f"unsupported event version: {event.version}")
        if event.column_id not in self.column_ids:
            raise EventError(f"unknown column: {event.column_id}")
        if not self.store.challenge_exists(event.challenge_id):
            raise EventError(f"unknown challenge: {event.challenge_id}; run `ctfdash sync` first")
        if event.type in LOG_EVENTS and not isinstance(event.payload.get("content"), str):
            raise EventError("log event content is required")

        stored_payload = dict(event.payload)
        if event.type in LOG_EVENTS:
            stored_payload.pop("content", None)
        with self.store.db:
            inserted = self.store.insert_event(
                event.event_id,
                event.type,
                event.occurred_at,
                event.challenge_id,
                event.column_id,
                stored_payload,
            )
            if not inserted:
                return "duplicate"
            if event.type in TASK_EVENTS:
                self._apply_task(event)
            elif event.type == "candidate":
                self._apply_candidate(event)
        if event.type in LOG_EVENTS:
            self._append_log(event)
        return "applied"

    def _task_or_create(self, event: Event) -> Any:
        task = self.store.task(event.challenge_id, event.column_id)
        if task:
            return task
        if event.type not in ("task.queued", "task.started", "task.excluded"):
            raise EventError("task does not exist; emit task.queued or task.started first")
        self.store.db.execute(
            "INSERT INTO tasks(challenge_id,column_id,status,queued_at) VALUES(?,?,?,?)",
            (event.challenge_id, event.column_id, "queued", event.occurred_at),
        )
        return self.store.task(event.challenge_id, event.column_id)

    def _apply_task(self, event: Event) -> None:
        task = self._task_or_create(event)
        assert task is not None
        status = str(task["status"])
        key = (event.challenge_id, event.column_id)
        if event.type == "task.queued":
            self.store.db.execute(
                """UPDATE tasks SET status='queued',queued_at=?,started_at=NULL,ended_at=NULL,
                   solve_seconds=NULL,failure_reason=NULL,session_index=0,candidate=NULL
                   WHERE challenge_id=? AND column_id=?""",
                (event.occurred_at, *key),
            )
            return
        if event.type == "task.excluded":
            self.store.db.execute(
                """UPDATE tasks SET status='excluded',ended_at=?,failure_reason=?
                   WHERE challenge_id=? AND column_id=?""",
                (event.occurred_at, str(event.payload.get("reason") or "operator"), *key),
            )
            return
        if event.type == "task.started":
            if status != "queued":
                raise EventError(f"task.started requires queued status, got {status}")
            self.store.db.execute(
                """UPDATE tasks SET status='running',started_at=?,ended_at=NULL,
                   solve_seconds=NULL,failure_reason=NULL,session_index=?
                   WHERE challenge_id=? AND column_id=?""",
                (event.occurred_at, int(event.payload.get("index", 1)), *key),
            )
            return
        if status != "running":
            raise EventError(f"{event.type} requires running status, got {status}")
        if event.type == "task.session":
            index = int(event.payload.get("index", 0))
            if index < 1:
                raise EventError("session index must be at least 1")
            self.store.db.execute(
                "UPDATE tasks SET session_index=? WHERE challenge_id=? AND column_id=?",
                (index, *key),
            )
        elif event.type == "task.succeeded":
            candidate = str(event.payload.get("candidate") or "") or None
            duration = ensure_started_before(str(task["started_at"]), event.occurred_at)
            self.store.db.execute(
                """UPDATE tasks SET status='success',ended_at=?,solve_seconds=?,
                   failure_reason=NULL,candidate=? WHERE challenge_id=? AND column_id=?""",
                (event.occurred_at, duration, candidate, *key),
            )
            if candidate:
                self.store.db.execute(
                    """INSERT OR REPLACE INTO candidates(
                       event_id,challenge_id,column_id,candidate,detected_at,verdict,source
                       ) VALUES(?,?,?,?,?,'verified','task.succeeded')""",
                    (event.event_id, event.challenge_id, event.column_id, candidate, event.occurred_at),
                )
        elif event.type == "task.failed":
            ensure_started_before(str(task["started_at"]), event.occurred_at)
            self.store.db.execute(
                """UPDATE tasks SET status='failed',ended_at=?,failure_reason=?
                   WHERE challenge_id=? AND column_id=?""",
                (
                    event.occurred_at,
                    str(event.payload.get("reason") or "unspecified"),
                    *key,
                ),
            )

    def _apply_candidate(self, event: Event) -> None:
        value = str(event.payload.get("value") or "")
        if not value:
            raise EventError("candidate value is required")
        verdict = str(event.payload.get("verdict") or "pending")
        source = str(event.payload.get("source") or "")
        self.store.db.execute(
            """INSERT INTO candidates(
               event_id,challenge_id,column_id,candidate,detected_at,verdict,source
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                event.event_id,
                event.challenge_id,
                event.column_id,
                value,
                event.occurred_at,
                verdict,
                source,
            ),
        )

    def _append_log(self, event: Event) -> None:
        content = event.payload.get("content")
        assert isinstance(content, str)
        task_id = f"{event.challenge_id}-{event.column_id}"
        path = self.config.log_dir / f"{task_id}.jsonl"
        record = {
            "version": 1,
            "event_id": event.event_id,
            "type": event.type,
            "occurred_at": event.occurred_at,
            "status": event.payload.get("status"),
            "content": content,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                handle.write(json_dumps(record) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            pass


def ingest_lines(engine: EventEngine, lines: Iterable[str]) -> dict[str, int]:
    result = {"applied": 0, "duplicate": 0}
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
            if not isinstance(raw, dict):
                raise EventError("event must be a JSON object")
            status = engine.apply(Event.from_dict(raw))
            result[status] += 1
        except (json.JSONDecodeError, EventError, StoreError) as exc:
            raise EventError(f"line {line_number}: {exc}") from exc
    return result

