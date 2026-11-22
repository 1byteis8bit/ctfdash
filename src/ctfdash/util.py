from __future__ import annotations

import datetime as dt
import json
import os
import re
import tempfile
import unicodedata
from pathlib import Path
from typing import Any

UTC = dt.timezone.utc


def utc_now() -> dt.datetime:
    return dt.datetime.now(UTC)


def iso_now() -> str:
    return utc_now().isoformat(timespec="milliseconds")


def parse_time(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a UTC offset")
    return parsed.astimezone(UTC)


def normalize_time(value: str | None) -> str:
    if value is None or value.strip().lower() == "now":
        return iso_now()
    return parse_time(value.strip()).isoformat(timespec="milliseconds")


def atomic_write(path: Path, content: str, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def text_width(value: str) -> int:
    return sum(2 if unicodedata.east_asian_width(char) in ("W", "F") else 1 for char in value)


def fit_text(value: str, width: int) -> str:
    if width <= 0:
        return ""
    if text_width(value) <= width:
        return value + " " * (width - text_width(value))
    suffix = "…"
    kept: list[str] = []
    used = text_width(suffix)
    for char in value:
        char_width = 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
        if used + char_width > width:
            break
        kept.append(char)
        used += char_width
    result = "".join(kept) + suffix
    return result + " " * max(0, width - text_width(result))


def format_duration(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, remainder = divmod(seconds, 60)
    if minutes < 60:
        return f"{int(minutes)}m {remainder:04.1f}s"
    hours, minutes = divmod(int(minutes), 60)
    return f"{hours}h {minutes:02d}m"


def format_signed_duration(seconds: float) -> str:
    return ("+" if seconds >= 0 else "-") + format_duration(abs(seconds))


def slug_id(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip("-.")
    if not normalized:
        raise ValueError("ID must contain a letter or number")
    return normalized


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

