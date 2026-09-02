"""讀取 LINE Messaging API 的月配額用量（純唯讀，不會發送任何訊息）。

只呼叫兩個 GET endpoint：
  GET /v2/bot/message/quota            → 方案配額上限
  GET /v2/bot/message/quota/consumption → 本月已用則數

Token 不寫進原始碼，依序從以下來源找（找到就用）：
  1. 環境變數 NOTICE_LINE_CHANNEL_ACCESS_TOKEN
  2. ~/.secrets/usage.env 內的 NOTICE_LINE_CHANNEL_ACCESS_TOKEN=...
  3. ~/.secrets/credentials.md 內的 NOTICE_LINE_CHANNEL_ACCESS_TOKEN="..."
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

QUOTA_URL = "https://api.line.me/v2/bot/message/quota"
CONSUMPTION_URL = "https://api.line.me/v2/bot/message/quota/consumption"
TOKEN_ENV = "NOTICE_LINE_CHANNEL_ACCESS_TOKEN"
ENV_FILE = Path(os.path.expanduser("~/.secrets/usage.env"))
CREDENTIALS_FILE = Path(os.path.expanduser("~/.secrets/credentials.md"))

CACHE_TTL_SECONDS = 600.0
REQUEST_TIMEOUT = 6.0

_lock = threading.Lock()
_cache: tuple[float, LineQuotaResult] | None = None


@dataclass(slots=True)
class LineQuotaResult:
    available: bool
    used: int = 0
    limit: int | None = None
    percent: float | None = None
    resets_at: float | None = None
    message: str = ""


def _token_from_env_file(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(rf'^{TOKEN_ENV}\s*=\s*"?([^"\n]+)"?\s*$', text, re.MULTILINE)
    return match.group(1).strip() if match else None


def _token_from_credentials(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(rf'{TOKEN_ENV}="([^"]+)"', text)
    return match.group(1).strip() if match else None


def load_token() -> str | None:
    token = os.environ.get(TOKEN_ENV)
    if token and token.strip():
        return token.strip()
    return _token_from_env_file(ENV_FILE) or _token_from_credentials(CREDENTIALS_FILE)


def _get_json(url: str, token: str) -> dict:
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


def next_month_reset(now: float | None = None) -> float:
    """LINE 的月配額在每月 1 號 00:00（帳號時區）歸零，這裡用本地時區近似。"""
    current = datetime.fromtimestamp(now if now is not None else time.time()).astimezone()
    first_of_next = (current.replace(day=28) + timedelta(days=7)).replace(
        day=1,
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    return first_of_next.timestamp()


def fetch(*, mock: bool = False, force: bool = False) -> LineQuotaResult:
    """回傳 Notice bot 的本月配額用量；失敗時 available=False，UI 顯示 --。"""
    global _cache

    if mock:
        return LineQuotaResult(
            available=True,
            used=137,
            limit=200,
            percent=68.5,
            resets_at=next_month_reset(),
            message="已同步",
        )

    now = time.time()
    with _lock:
        if not force and _cache is not None and (now - _cache[0]) < CACHE_TTL_SECONDS:
            return _cache[1]

    token = load_token()
    if not token:
        result = LineQuotaResult(available=False, message="找不到 Notice bot token")
    else:
        try:
            quota = _get_json(QUOTA_URL, token)
            consumption = _get_json(CONSUMPTION_URL, token)
        except urllib.error.HTTPError as exc:
            result = LineQuotaResult(available=False, message=f"LINE API {exc.code}")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            result = LineQuotaResult(available=False, message=f"LINE 連線失敗（{type(exc).__name__}）")
        else:
            result = _build_result(quota, consumption)

    with _lock:
        _cache = (time.time(), result)
    return result


def _build_result(quota: dict, consumption: dict) -> LineQuotaResult:
    used = int(consumption.get("totalUsage") or 0)
    limit_raw = quota.get("value")
    limit = int(limit_raw) if quota.get("type") == "limited" and limit_raw is not None else None
    percent = None
    if limit:
        percent = max(0.0, min(100.0, used / limit * 100.0))
    return LineQuotaResult(
        available=True,
        used=used,
        limit=limit,
        percent=percent,
        resets_at=next_month_reset(),
        message="已同步",
    )
