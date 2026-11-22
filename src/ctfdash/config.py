from __future__ import annotations

import datetime as dt
import os
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .util import atomic_write, slug_id, toml_string

CONFIG_NAME = "ctfdash.toml"
DEFAULT_FLAG_REGEX = r"(?:[A-Za-z0-9_]+)\{[^\s{}]{1,512}\}"


class ConfigError(RuntimeError):
    pass


class InitCancelled(RuntimeError):
    pass


@dataclass(frozen=True)
class Column:
    id: str
    label: str
    order: int


@dataclass(frozen=True)
class Config:
    root: Path
    title: str
    base_url: str
    start: dt.datetime
    end: dt.datetime
    timezone: str
    flag_regex: str
    state_dir: Path
    token_env: str | None
    session_file: Path | None
    columns: tuple[Column, ...]
    excluded_categories: frozenset[str]
    excluded_challenge_ids: frozenset[int]
    refresh_seconds: float

    @property
    def database(self) -> Path:
        return self.state_dir / "state.sqlite3"

    @property
    def log_dir(self) -> Path:
        return self.state_dir / "logs"


def find_project(explicit: str | None = None, start: Path | None = None) -> Path:
    if explicit:
        candidate = Path(explicit).expanduser().resolve()
        config = candidate if candidate.name == CONFIG_NAME else candidate / CONFIG_NAME
        if not config.is_file():
            raise ConfigError(f"No {CONFIG_NAME} at {config}")
        return config
    environment = os.environ.get("CTFDASH_PROJECT")
    if environment:
        return find_project(environment)
    current = (start or Path.cwd()).resolve()
    for directory in (current, *current.parents):
        config = directory / CONFIG_NAME
        if config.is_file():
            return config
    raise ConfigError(f"No {CONFIG_NAME} found; run `ctfdash init` first")


def load_config(path: Path) -> Config:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        project = raw["project"]
        ctfd = raw["ctfd"]
        display = raw["display"]
        raw_columns = raw["columns"]
        columns = tuple(
            sorted(
                (Column(slug_id(str(item["id"])), str(item["label"]), int(item.get("order", index)))
                 for index, item in enumerate(raw_columns)),
                key=lambda item: item.order,
            )
        )
        if not columns or len({column.id for column in columns}) != len(columns):
            raise ValueError("columns must contain unique IDs")
        start = dt.datetime.fromisoformat(str(project["start_utc"]).replace("Z", "+00:00"))
        end = dt.datetime.fromisoformat(str(project["end_utc"]).replace("Z", "+00:00"))
        if start.tzinfo is None or end.tzinfo is None or end <= start:
            raise ValueError("contest timestamps are invalid")
        title = str(project["title"]).strip()
        base_url = str(ctfd["base_url"]).rstrip("/")
        if not title:
            raise ValueError("project title is empty")
        _validate_url(base_url)
        ZoneInfo(str(project["timezone"]))
        re.compile(str(project["flag_regex"]))
        refresh_seconds = float(display.get("refresh_seconds", 2.0))
        if refresh_seconds < 0.1:
            raise ValueError("refresh_seconds must be at least 0.1")
        if not ctfd.get("token_env") and not ctfd.get("session_file"):
            raise ValueError("at least one CTFd authentication method is required")
        if ctfd.get("token_env"):
            _validate_env_name(str(ctfd["token_env"]))
        root = path.parent.resolve()
        state_dir = (root / str(project.get("state_dir", ".ctfdash"))).resolve()
        session_value = ctfd.get("session_file")
        session_file = (root / str(session_value)).resolve() if session_value else None
        return Config(
            root=root,
            title=title,
            base_url=base_url,
            start=start.astimezone(dt.timezone.utc),
            end=end.astimezone(dt.timezone.utc),
            timezone=str(project["timezone"]),
            flag_regex=str(project["flag_regex"]),
            state_dir=state_dir,
            token_env=str(ctfd["token_env"]) if ctfd.get("token_env") else None,
            session_file=session_file,
            columns=columns,
            excluded_categories=frozenset(
                str(value).strip().lower() for value in display.get("excluded_categories", [])
            ),
            excluded_challenge_ids=frozenset(
                int(value) for value in display.get("excluded_challenge_ids", [])
            ),
            refresh_seconds=refresh_seconds,
        )
    except (KeyError, TypeError, ValueError, re.error, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"Invalid {path}: {exc}") from exc


def _ask(prompt: str, default: str | None = None, validator=None) -> str:
    suffix = f" [{default}]" if default is not None else ""
    while True:
        value = input(f"{prompt}{suffix}: ").strip()
        if not value and default is not None:
            value = default
        if not value:
            print("값을 입력해야 합니다.")
            continue
        try:
            if validator:
                validator(value)
            return value
        except (ValueError, re.error, ZoneInfoNotFoundError) as exc:
            print(f"잘못된 값: {exc}")


def _ask_choice(prompt: str, choices: dict[str, str], default: str) -> str:
    rendered = ", ".join(f"{key}={label}" for key, label in choices.items())
    while True:
        value = input(f"{prompt} ({rendered}) [{default}]: ").strip() or default
        if value in choices:
            return value
        print("목록에 있는 값을 선택하세요.")


def _ask_yes_no(prompt: str, default: bool = True) -> bool:
    marker = "Y/n" if default else "y/N"
    while True:
        value = input(f"{prompt} [{marker}]: ").strip().lower()
        if not value:
            return default
        if value in ("y", "yes", "예"):
            return True
        if value in ("n", "no", "아니오"):
            return False
        print("y 또는 n으로 답하세요.")


def _validate_url(value: str) -> None:
    if not re.fullmatch(r"https?://[^\s/]+(?:/[^\s]*)?", value):
        raise ValueError("http:// 또는 https:// URL이 필요합니다")


def _validate_env_name(value: str) -> None:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError("환경변수 이름 형식이 아닙니다")


def _local_time(value: str, timezone: ZoneInfo) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone)
    return parsed.astimezone(dt.timezone.utc)


def _csv_strings(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _csv_ints(value: str) -> list[int]:
    if not value.strip():
        return []
    return [int(item.strip()) for item in value.split(",") if item.strip()]


def init_project(target: Path) -> Path:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise ConfigError("`ctfdash init` requires an interactive terminal")
    target = target.expanduser().resolve()
    config_path = target / CONFIG_NAME
    if config_path.exists():
        action = _ask_choice("기존 설정이 있습니다", {"r": "재설정", "c": "취소"}, "c")
        if action == "c":
            raise InitCancelled("초기 설정을 취소했습니다")

    print("\nCTFd 대시보드 초기 설정\n")
    title = _ask("대회 표시 이름")
    base_url = _ask("CTFd 기본 URL", validator=_validate_url).rstrip("/")
    timezone_name = _ask("대회 시간대(IANA)", "UTC", ZoneInfo)
    timezone = ZoneInfo(timezone_name)
    start_text = _ask("대회 시작 일시(YYYY-MM-DD HH:MM 또는 ISO 8601)")
    start = _local_time(start_text, timezone)
    while True:
        end_text = _ask("대회 종료 일시(YYYY-MM-DD HH:MM 또는 ISO 8601)")
        end = _local_time(end_text, timezone)
        if end > start:
            break
        print("종료 시각은 시작 시각보다 뒤여야 합니다.")
    flag_regex = _ask("flag 정규식", DEFAULT_FLAG_REGEX, re.compile)
    auth = _ask_choice("인증 방식", {"1": "API 토큰", "2": "쿠키 세션", "3": "둘 다"}, "1")
    token_env = (
        _ask("API 토큰 환경변수", "CTFD_TOKEN", _validate_env_name)
        if auth in ("1", "3") else ""
    )
    session_file = _ask("쿠키 세션 파일", ".ctfdash/session.json") if auth in ("2", "3") else ""

    print("\n표시 열을 입력하세요. 최소 한 개가 필요합니다.")
    columns: list[Column] = []
    while True:
        default_id = "MAIN" if not columns else None
        raw_id = input(f"열 ID{f' [{default_id}]' if default_id else ' (빈 입력=완료)'}: ").strip()
        if not raw_id and default_id:
            raw_id = default_id
        elif not raw_id:
            if columns:
                break
            print("최소 한 개의 열이 필요합니다.")
            continue
        try:
            column_id = slug_id(raw_id)
        except ValueError as exc:
            print(f"잘못된 값: {exc}")
            continue
        if any(column.id == column_id for column in columns):
            print("이미 사용 중인 열 ID입니다.")
            continue
        label = _ask("표시 이름", column_id)
        columns.append(Column(column_id, label, len(columns)))

    excluded_categories = _csv_strings(input("집계 제외 분야(쉼표 구분, 없으면 빈 입력): ").strip())
    while True:
        raw_ids = input("제외할 문제 ID(쉼표 구분, 없으면 빈 입력): ").strip()
        try:
            excluded_ids = _csv_ints(raw_ids)
            break
        except ValueError:
            print("문제 ID는 정수여야 합니다.")
    while True:
        refresh_raw = input("화면 새로고침 초 [2.0]: ").strip() or "2.0"
        try:
            refresh = float(refresh_raw)
            if refresh < 0.1:
                raise ValueError
            break
        except ValueError:
            print("0.1 이상의 숫자가 필요합니다.")

    print("\n설정 요약")
    print(f"  대회: {title}")
    print(f"  CTFd: {base_url}")
    print(f"  기간: {start.isoformat()} ~ {end.isoformat()}")
    print(f"  열: {', '.join(f'{item.id}={item.label}' for item in columns)}")
    print(f"  인증: {'토큰 ' if token_env else ''}{'쿠키' if session_file else ''}")
    if not _ask_yes_no("이 설정을 저장할까요?", True):
        raise InitCancelled("초기 설정을 취소했습니다")

    lines = [
        "[project]",
        f"title = {toml_string(title)}",
        f"timezone = {toml_string(timezone_name)}",
        f"start_utc = {toml_string(start.isoformat(timespec='seconds'))}",
        f"end_utc = {toml_string(end.isoformat(timespec='seconds'))}",
        f"flag_regex = {toml_string(flag_regex)}",
        'state_dir = ".ctfdash"',
        "",
        "[ctfd]",
        f"base_url = {toml_string(base_url)}",
        f"token_env = {toml_string(token_env)}",
        f"session_file = {toml_string(session_file)}",
        "",
        "[display]",
        f"excluded_categories = {repr(excluded_categories)}",
        f"excluded_challenge_ids = {repr(excluded_ids)}",
        f"refresh_seconds = {refresh}",
        "",
    ]
    for column in columns:
        lines.extend(
            [
                "[[columns]]",
                f"id = {toml_string(column.id)}",
                f"label = {toml_string(column.label)}",
                f"order = {column.order}",
                "",
            ]
        )
    target.mkdir(parents=True, exist_ok=True)
    atomic_write(config_path, "\n".join(lines), 0o600)
    state_dir = target / ".ctfdash"
    (state_dir / "logs").mkdir(parents=True, exist_ok=True)
    state_dir.chmod(0o700)
    return config_path

