from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import sys
import termios
import textwrap
from pathlib import Path
from typing import Any

from .config import Config
from .store import Store
from .util import fit_text, format_duration, format_signed_duration, parse_time, text_width, utc_now

MOUSE_RE = re.compile(rb"\x1b\[<(\d+);(\d+);(\d+)([Mm])")

COLORS = {
    "green": "\033[32m",
    "red": "\033[31m",
    "yellow": "\033[33m",
    "gray": "\033[90m",
    "cyan": "\033[36m",
    "magenta": "\033[35m",
    "bold": "\033[1m",
    "reset": "\033[0m",
}


def painted(value: str, color: str, use_color: bool) -> str:
    return f"{COLORS[color]}{value}{COLORS['reset']}" if use_color else value


def load_payload(config: Config) -> dict[str, Any]:
    if not config.database.exists():
        return {"ok": False, "title": config.title, "error": "아직 상태 DB가 없습니다. `ctfdash sync`를 실행하세요."}
    with Store(config) as store:
        return store.status_payload()


def format_status(
    config: Config,
    payload: dict[str, Any],
    use_color: bool | None = None,
    hit_map: list[tuple[int, int, int, str]] | None = None,
) -> str:
    if hit_map is not None:
        hit_map.clear()
    if use_color is None:
        use_color = sys.stdout.isatty() and "NO_COLOR" not in os.environ
    if not payload.get("ok"):
        return f"{payload.get('title', config.title)} 대시보드\n\n{payload.get('error', '상태를 읽을 수 없습니다')}"

    now = parse_time(str(payload["now"]))
    remaining = max(0.0, (config.end - now).total_seconds())
    columns = list(config.columns)
    challenges = {int(row["id"]): row for row in payload.get("challenges", [])}
    tasks = {
        (int(row["challenge_id"]), str(row["column_id"])): row
        for row in payload.get("tasks", [])
    }
    top1 = {int(key): value for key, value in payload.get("top1", {}).items()}

    def aggregate_challenge(challenge_id: int) -> bool:
        row = challenges[challenge_id]
        return (
            challenge_id not in config.excluded_challenge_ids
            and str(row.get("category") or "").lower() not in config.excluded_categories
        )

    total = sum(aggregate_challenge(challenge_id) for challenge_id in challenges)
    counts: dict[str, dict[str, int]] = {column.id: {} for column in columns}
    for challenge_id in challenges:
        if not aggregate_challenge(challenge_id):
            continue
        for column in columns:
            status = str(tasks.get((challenge_id, column.id), {}).get("status", "missing"))
            counts[column.id][status] = counts[column.id].get(status, 0) + 1

    terminal_width = shutil.get_terminal_size((140, 35)).columns
    status_width = 19
    delta_width = 14
    name_width = max(22, min(52, terminal_width - len(columns) * (status_width + delta_width + 6) - 3))
    rendered_width = name_width + 3 + len(columns) * (status_width + delta_width + 6) - 3
    separator = "─" * max(20, rendered_width)

    summaries = []
    for column in columns:
        values = counts[column.id]
        summaries.append(
            f"{column.label} {values.get('success', 0)}/{total} 성공 · "
            f"{values.get('failed', 0)} 실패 · {values.get('running', 0)} 실행"
        )
    lines = [
        painted(f"{config.title} 실시간 대시보드", "bold", use_color),
        f"UTC {now.strftime('%Y-%m-%d %H:%M:%S')}  │  종료까지 {format_duration(remaining)}  │  "
        f"마지막 동기화 {payload.get('last_sync_at') or '-'}",
        f"전체 문제 {len(challenges)}  │  통계 대상 {total}  │  집계 제외 {len(challenges) - total}",
        "  │  ".join(summaries),
        separator,
        f"{fit_text('문제', name_width)} │ "
        + " │ ".join(
            cell
            for column in columns
            for cell in (
                fit_text(f"{column.label} 상태", status_width),
                fit_text(f"{column.label}−Top1", delta_width),
            )
        ),
        separator,
    ]

    def status_cell(task: dict[str, Any] | None) -> str:
        if not task:
            plain, color = "⚠ task 누락", "red"
        else:
            status = str(task.get("status"))
            if status == "success":
                plain, color = f"● 성공 {format_duration(float(task.get('solve_seconds') or 0))}", "green"
            elif status == "failed":
                reason = str(task.get("failure_reason") or "")
                timeout = re.search(r"timeout[_-]?(\d+)([msh]?)", reason)
                plain = f"● 실패 {timeout.group(1)}{timeout.group(2)}" if timeout else "● 실패"
                color = "red"
            elif status == "running":
                try:
                    elapsed = (now - parse_time(str(task["started_at"]))).total_seconds()
                    plain = f"● 실행 S{int(task.get('session_index') or 1)} {format_duration(elapsed)}"
                except (ValueError, TypeError, KeyError):
                    plain = "● 실행 중"
                color = "yellow"
            elif status == "excluded":
                plain, color = "— 제외", "gray"
            else:
                plain, color = "○ 대기", "gray"
        return painted(fit_text(plain, status_width), color, use_color)

    def delta_cell(challenge_id: int, task: dict[str, Any] | None) -> str:
        comparator = top1.get(challenge_id)
        if not task or task.get("status") != "success":
            plain, color = "-", "gray"
        elif not comparator:
            plain, color = "Top1 없음", "gray"
        else:
            delta = float(task.get("solve_seconds") or 0) - float(comparator["elapsed_seconds"])
            rounded = round(delta, 1)
            if rounded < 0:
                plain, color = format_signed_duration(delta), "green"
            elif rounded > 0:
                plain, color = format_signed_duration(delta), "red"
            else:
                plain, color = "0.0s", "gray"
        return painted(fit_text(plain, delta_width), color, use_color)

    def sort_key(challenge_id: int) -> tuple[int, int]:
        if not aggregate_challenge(challenge_id):
            return (2, challenge_id)
        active = [tasks.get((challenge_id, column.id)) for column in columns]
        fully_solved = all(task and task.get("status") == "success" for task in active)
        return (1 if fully_solved else 0, challenge_id)

    categories: dict[str, list[int]] = {}
    for challenge_id, challenge in challenges.items():
        category = str(challenge.get("category") or "uncategorized").lower()
        categories.setdefault(category, []).append(challenge_id)
    category_order = sorted(categories, key=lambda value: (value in config.excluded_categories, value))
    for category in category_order:
        excluded = " · 집계 제외" if category in config.excluded_categories else ""
        lines.append(painted(f"[ {category.upper()} · {len(categories[category])}문제{excluded} ]", "cyan", use_color))
        for challenge_id in sorted(categories[category], key=sort_key):
            challenge = challenges[challenge_id]
            row_number = len(lines) + 1
            row_cells: list[str] = []
            logical_column = name_width + 4
            for column in columns:
                task = tasks.get((challenge_id, column.id))
                row_cells.extend((status_cell(task), delta_cell(challenge_id, task)))
                if hit_map is not None:
                    hit_map.append(
                        (
                            row_number,
                            logical_column,
                            logical_column + status_width + 3 + delta_width - 1,
                            f"{challenge_id}-{column.id}",
                        )
                    )
                logical_column += status_width + 3 + delta_width + 3
            label = f"[{challenge_id}] {challenge['name']}"
            lines.append(f"{fit_text(label, name_width)} │ " + " │ ".join(row_cells))

    fallbacks = payload.get("submission_fallbacks") or []
    if fallbacks:
        lines.extend((separator, painted("[ 제출 오류 · 수동 제출 후보 ]", "red", use_color)))
        for candidate in fallbacks:
            lines.append(
                painted(
                    f"[{candidate['challenge_id']}] {candidate['challenge']} "
                    f"({candidate['column_id']}) · {candidate['verdict']} · {candidate.get('source') or '-'}",
                    "yellow",
                    use_color,
                )
            )
            lines.append(f"    {candidate['candidate']}")

    lines.extend(
        (
            separator,
            f"{painted('●', 'green', use_color)} 성공  {painted('●', 'red', use_color)} 실패  "
            f"{painted('●', 'yellow', use_color)} 실행 중  {painted('○', 'gray', use_color)} 대기",
            f"Top1 시간차: {painted('음수=더 빠름', 'green', use_color)}  "
            f"{painted('양수=더 느림', 'red', use_color)}  {painted('0.0s=동일', 'gray', use_color)}",
            "종료: Ctrl-C 또는 q",
        )
    )
    return "\n".join(lines)


def _style(value: str, code: str) -> str:
    if "NO_COLOR" in os.environ or not sys.stdout.isatty():
        return value
    return f"\033[{code}m{value}\033[0m"


def _highlight_flags(config: Config, value: str) -> str:
    regex = re.compile(config.flag_regex)
    return regex.sub(lambda match: _style(match.group(0), "1;32"), value)


def _load_log(path: Path, limit: int = 8 * 1024 * 1024) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        start = max(0, size - limit)
        handle.seek(start)
        data = handle.read()
    if start:
        newline = data.find(b"\n")
        data = data[newline + 1:] if newline >= 0 else b""
    events = []
    for line in data.decode("utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                events.append(value)
        except json.JSONDecodeError:
            continue
    return events


def _panel(config: Config, label: str, timestamp: str, content: str, width: int, code: str) -> list[str]:
    try:
        clock = parse_time(timestamp).strftime("%H:%M:%S")
    except (ValueError, TypeError):
        clock = timestamp
    title = f"┌─ {label} · {clock} "
    lines = [_style(title + "─" * max(0, width - text_width(title)), code)]
    for paragraph in content.splitlines() or [""]:
        wrapped = textwrap.wrap(
            paragraph,
            width=max(20, width - 3),
            replace_whitespace=False,
            drop_whitespace=False,
            break_long_words=True,
            break_on_hyphens=False,
        ) or [""]
        lines.extend(_style("│ ", code) + _highlight_flags(config, line) for line in wrapped)
    lines.append(_style("└" + "─" * max(0, width - 1), code))
    return lines


def render_log(config: Config, events: list[dict[str, Any]], width: int, mode: int) -> list[str]:
    if mode == 3:
        raw = "\n".join(json.dumps(event, ensure_ascii=False) for event in events)
        result: list[str] = []
        for line in raw.splitlines():
            result.extend(textwrap.wrap(line, max(20, width), break_long_words=True) or [""])
        return result or ["로그 이벤트를 기다리는 중…"]
    rendered: list[str] = []
    latest_codex: dict[str, Any] | None = None
    for event in events:
        event_type = str(event.get("type"))
        content = str(event.get("content") or "")
        timestamp = str(event.get("occurred_at") or "")
        if event_type == "log.codex":
            latest_codex = event
            rendered.extend(_panel(config, "◆ CODEX", timestamp, content, width, "1;36"))
        elif event_type == "log.reasoning" and mode == 1:
            rendered.extend(_panel(config, "◇ REASONING", timestamp, content, width, "35"))
        elif mode == 1 and event_type == "log.command":
            first = content.splitlines()[0] if content else "command"
            rendered.extend(_style(line, "33") for line in textwrap.wrap(f"▶ SHELL  {first}", max(20, width)))
        elif mode == 1 and event_type == "log.result":
            error = str(event.get("status")) == "error"
            marker, code = ("✖ RESULT", "1;31") if error else ("✓ RESULT", "2;37")
            rendered.append(_style(marker, code))
            meaningful = [line for line in content.splitlines() if line.strip()]
            excerpt = meaningful if len(meaningful) <= 6 else [*meaningful[:3], "…", *meaningful[-2:]]
            for line in excerpt:
                for part in textwrap.wrap(line, max(20, width - 4), break_long_words=True) or [""]:
                    rendered.append(_style("  " + _highlight_flags(config, part), code))
    if mode == 1 and latest_codex:
        rendered.extend(
            (
                _style("═" * width, "1;36"),
                *_panel(
                    config,
                    "★ LATEST CODEX",
                    str(latest_codex.get("occurred_at") or ""),
                    str(latest_codex.get("content") or ""),
                    width,
                    "1;36",
                ),
            )
        )
    return rendered or ["로그 이벤트를 기다리는 중…"]


def _task_snapshot(config: Config, task_id: str) -> dict[str, Any] | None:
    try:
        challenge_text, column_id = task_id.split("-", 1)
        challenge_id = int(challenge_text)
    except ValueError:
        return None
    with Store(config) as store:
        row = store.db.execute(
            """SELECT t.*,c.name AS challenge,c.category FROM tasks t
               JOIN challenges c ON c.id=t.challenge_id
               WHERE t.challenge_id=? AND t.column_id=?""",
            (challenge_id, column_id),
        ).fetchone()
        return dict(row) if row else None


async def task_viewer(config: Config, task_id: str, input_queue: asyncio.Queue[bytes]) -> None:
    scroll = 0
    mode = 1
    while True:
        width, height = shutil.get_terminal_size((120, 35))
        snapshot = _task_snapshot(config, task_id)
        events = _load_log(config.log_dir / f"{task_id}.jsonl")
        log_lines = render_log(config, events, width, mode)
        if snapshot:
            status = str(snapshot["status"])
            if snapshot.get("solve_seconds") is not None:
                timing = format_duration(float(snapshot["solve_seconds"]))
            elif snapshot.get("started_at"):
                end = parse_time(str(snapshot["ended_at"])) if snapshot.get("ended_at") else utc_now()
                timing = format_duration((end - parse_time(str(snapshot["started_at"]))).total_seconds())
            else:
                timing = "-"
            marker = {
                "running": ("● RUNNING", "1;33"),
                "success": ("● SUCCESS", "1;32"),
                "failed": ("● FAILED", "1;31"),
                "excluded": ("○ EXCLUDED", "2;37"),
                "queued": ("○ QUEUED", "2;37"),
            }.get(status, (status.upper(), "2;37"))
            heading = f"{marker[0]}  [{task_id}] {snapshot['challenge']} ({snapshot['category']})  {timing}"
            detail = f"session={snapshot.get('session_index') or 0}"
            if snapshot.get("failure_reason"):
                detail += f"  reason={snapshot['failure_reason']}"
        else:
            marker = ("NOT FOUND", "1;31")
            heading, detail = f"[{task_id}] task 정보를 찾을 수 없습니다", ""
        controls = "1:집중  2:Codex만  3:원문 │ PgUp/PgDn/↑/↓:스크롤 f:최신 Ctrl+C/q/Esc:복귀"
        body_height = max(3, height - 5)
        scroll = min(scroll, max(0, len(log_lines) - 1))
        end_index = max(0, len(log_lines) - scroll)
        start_index = max(0, end_index - body_height)
        visible = log_lines[start_index:end_index]
        screen = [
            _style(fit_text(heading, width), marker[1]),
            _style(fit_text(detail, width), "2;37"),
            fit_text(f"{controls} [{'LIVE' if scroll == 0 else f'-{scroll} lines'}]", width),
            _style("─" * width, "2;37"),
            *(visible or ["로그 이벤트를 기다리는 중…"]),
        ]
        sys.stdout.write("\033[2J\033[H" + "\n".join(screen))
        sys.stdout.flush()
        try:
            data = await asyncio.wait_for(input_queue.get(), timeout=0.5)
        except asyncio.TimeoutError:
            continue
        if b"\x03" in data or data in (b"q", b"Q", b"\x1b"):
            return
        if data in (b"1", b"2", b"3"):
            mode, scroll = int(data), 0
        elif b"\x1b[5~" in data:
            scroll += max(1, body_height - 2)
        elif b"\x1b[6~" in data:
            scroll = max(0, scroll - max(1, body_height - 2))
        elif b"\x1b[A" in data:
            scroll += 1
        elif b"\x1b[B" in data or data in (b"f", b"F"):
            scroll = max(0, scroll - 1) if b"\x1b[B" in data else 0


async def interactive_dashboard(config: Config, interval: float | None = None) -> int:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise RuntimeError("dashboard requires an interactive terminal; use `ctfdash status` instead")
    refresh = interval if interval is not None else config.refresh_seconds
    input_fd = sys.stdin.fileno()
    saved = termios.tcgetattr(input_fd)
    raw = termios.tcgetattr(input_fd)
    raw[3] &= ~(termios.ICANON | termios.ECHO | termios.ISIG)
    raw[6][termios.VMIN] = 1
    raw[6][termios.VTIME] = 0
    queue: asyncio.Queue[bytes] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def on_input() -> None:
        with contextlib.suppress(OSError):
            data = os.read(input_fd, 4096)
            if data:
                queue.put_nowait(data)

    termios.tcsetattr(input_fd, termios.TCSADRAIN, raw)
    loop.add_reader(input_fd, on_input)
    sys.stdout.write("\033[?1049h\033[?25l\033[?1000h\033[?1006h")
    sys.stdout.flush()
    try:
        scroll = 0
        mouse_buffer = b""
        while True:
            hit_map: list[tuple[int, int, int, str]] = []
            rendered = format_status(config, load_payload(config), hit_map=hit_map)
            rendered_lines = rendered.splitlines()
            _, height = shutil.get_terminal_size((140, 35))
            body_height = max(5, height - 1)
            max_scroll = max(0, len(rendered_lines) - body_height)
            scroll = min(scroll, max_scroll)
            visible = rendered_lines[scroll:scroll + body_height]
            footer = f"클릭: 로그 보기 │ wheel/PageUp/PageDown: scroll │ Ctrl+C/q: 종료 [{scroll + 1}-{scroll + len(visible)}/{len(rendered_lines)}]"
            sys.stdout.write("\033[2J\033[H" + "\n".join([*visible, footer]))
            sys.stdout.flush()
            try:
                data = await asyncio.wait_for(queue.get(), timeout=max(0.1, refresh))
            except asyncio.TimeoutError:
                continue
            if b"\x03" in data or data in (b"q", b"Q"):
                return 0
            page = max(1, body_height - 3)
            if b"\x1b[5~" in data:
                scroll = max(0, scroll - page)
            elif b"\x1b[6~" in data:
                scroll = min(max_scroll, scroll + page)
            elif b"\x1b[A" in data:
                scroll = max(0, scroll - 1)
            elif b"\x1b[B" in data:
                scroll = min(max_scroll, scroll + 1)
            mouse_buffer = (mouse_buffer + data)[-512:]
            matches = list(MOUSE_RE.finditer(mouse_buffer))
            if not matches:
                continue
            last_end = 0
            for match in matches:
                last_end = match.end()
                button, column, row = map(int, match.group(1, 2, 3))
                if match.group(4) != b"M":
                    continue
                if button == 64:
                    scroll = max(0, scroll - 3)
                    continue
                if button == 65:
                    scroll = min(max_scroll, scroll + 3)
                    continue
                if button != 0 or row > body_height:
                    continue
                absolute_row = row + scroll
                task_id = next(
                    (task for hit_row, start, end, task in hit_map if hit_row == absolute_row and start <= column <= end),
                    "",
                )
                if task_id:
                    while not queue.empty():
                        with contextlib.suppress(asyncio.QueueEmpty):
                            queue.get_nowait()
                    await task_viewer(config, task_id, queue)
                    break
            mouse_buffer = mouse_buffer[last_end:]
    finally:
        loop.remove_reader(input_fd)
        termios.tcsetattr(input_fd, termios.TCSADRAIN, saved)
        sys.stdout.write("\033[?1006l\033[?1000l\033[?25h\033[?1049l")
        sys.stdout.flush()

