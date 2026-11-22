from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from .config import Config
from .store import Store
from .util import parse_time


class CTFdError(RuntimeError):
    pass


@dataclass(frozen=True)
class SyncResult:
    challenges: int
    ranked_challenges: int
    account_id: int | None


class CTFdClient:
    def __init__(self, config: Config):
        self.config = config
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "ctfdash/0.1"})
        if config.token_env:
            token = os.environ.get(config.token_env)
            if not token:
                raise CTFdError(f"Required environment variable {config.token_env} is not set")
            self.session.headers["Authorization"] = f"Token {token}"
        if config.session_file:
            self._load_session(config.session_file)

    def close(self) -> None:
        self.session.close()

    def _load_session(self, path: Path) -> None:
        if not path.is_file():
            raise CTFdError(f"Cookie session file does not exist: {path}")
        if path.stat().st_mode & 0o077:
            raise CTFdError(f"Cookie session file must have mode 0600: {path}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            user_agent = payload.get("user_agent")
            if user_agent:
                self.session.headers["User-Agent"] = str(user_agent)
            cookies = payload.get("cookies") or []
            if isinstance(cookies, dict):
                cookies = [{"name": key, "value": value} for key, value in cookies.items()]
            for cookie in cookies:
                self.session.cookies.set(
                    str(cookie["name"]),
                    str(cookie["value"]),
                    domain=cookie.get("domain"),
                    path=str(cookie.get("path") or "/"),
                )
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise CTFdError(f"Invalid cookie session file: {path}") from exc

    def _request(self, path: str) -> Any:
        url = f"{self.config.base_url}{path}"
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                response = self.session.get(url, timeout=30)
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt == 0:
                        time.sleep(0.5)
                        continue
                if response.status_code in (401, 403):
                    raise CTFdError(f"CTFd authentication failed with HTTP {response.status_code}")
                if 400 <= response.status_code < 500 and response.status_code != 429:
                    raise CTFdError(f"CTFd request failed with HTTP {response.status_code} for {path}")
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict) or not payload.get("success"):
                    raise CTFdError(f"CTFd API returned an unsuccessful response for {path}")
                return payload.get("data")
            except CTFdError:
                raise
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                if attempt == 0:
                    time.sleep(0.5)
                    continue
        name = type(last_error).__name__ if last_error else "HTTPError"
        raise CTFdError(f"CTFd request failed for {path}: {name}") from last_error

    def own_account_id(self) -> int | None:
        data = self._request("/api/v1/users/me")
        if isinstance(data, dict):
            value = data.get("id")
            if value is None and isinstance(data.get("user"), dict):
                value = data["user"].get("id")
            return int(value) if value is not None else None
        return None

    def challenges(self) -> list[dict[str, Any]]:
        data = self._request("/api/v1/challenges")
        if not isinstance(data, list):
            raise CTFdError("CTFd challenge list has an unexpected shape")
        return [dict(item) for item in data]

    def challenge(self, challenge_id: int) -> dict[str, Any]:
        data = self._request(f"/api/v1/challenges/{challenge_id}")
        if not isinstance(data, dict):
            raise CTFdError(f"Challenge {challenge_id} has an unexpected shape")
        return dict(data)

    def solves(self, challenge_id: int) -> list[dict[str, Any]]:
        data = self._request(f"/api/v1/challenges/{challenge_id}/solves")
        if not isinstance(data, list):
            raise CTFdError(f"Challenge {challenge_id} solves have an unexpected shape")
        return [dict(item) for item in data]


def sync_ctfd(config: Config, store: Store) -> SyncResult:
    client = CTFdClient(config)
    try:
        account_id = client.own_account_id()
        listing = client.challenges()
        challenges: list[dict[str, Any]] = []
        ranked: dict[int, list[dict[str, Any]]] = {}
        for summary in listing:
            challenge_id = int(summary["id"])
            detail = client.challenge(challenge_id)
            challenges.append(
                {
                    "id": challenge_id,
                    "name": detail.get("name", summary.get("name", challenge_id)),
                    "category": detail.get("category", summary.get("category", "")),
                    "value": detail.get("value", summary.get("value", 0)),
                }
            )
            normalized: list[tuple[Any, int | None, str]] = []
            for raw in client.solves(challenge_id):
                raw_account = raw.get("account_id")
                solve_account = int(raw_account) if raw_account is not None else None
                if account_id is not None and solve_account == account_id:
                    continue
                solved_at = parse_time(str(raw.get("date") or raw.get("solved_at")))
                team_name = str(
                    raw.get("name")
                    or raw.get("team_name")
                    or raw.get("account_name")
                    or solve_account
                    or "unknown"
                )
                normalized.append((solved_at, solve_account, team_name))
            normalized.sort(key=lambda item: item[0])
            ranked[challenge_id] = [
                {
                    "rank": rank,
                    "account_id": solve_account,
                    "team_name": team_name,
                    "solved_at": solved_at.isoformat(timespec="milliseconds"),
                    "elapsed_seconds": max(0.0, (solved_at - config.start).total_seconds()),
                }
                for rank, (solved_at, solve_account, team_name) in enumerate(normalized[:3], 1)
            ]
        store.replace_sync(challenges, ranked, account_id)
        return SyncResult(
            challenges=len(challenges),
            ranked_challenges=sum(bool(values) for values in ranked.values()),
            account_id=account_id,
        )
    finally:
        client.close()

