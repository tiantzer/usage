"""讀取 LINE Messaging API 的月配額用量（純唯讀，不會發送任何訊息）。

每隻 bot 只呼叫三個 GET endpoint：
  GET /v2/bot/info                      → 確認 token 真的屬於這隻 bot
  GET /v2/bot/message/quota            → 方案配額上限
  GET /v2/bot/message/quota/consumption → 本月已用則數

Token 不寫進原始碼，依序從以下來源找（找到就用）：

Notice bot（@495vypyi）
  1. 環境變數 NOTICE_LINE_CHANNEL_ACCESS_TOKEN
  2. ~/.secrets/usage.env 內的 NOTICE_LINE_CHANNEL_ACCESS_TOKEN=...
  3. ~/.secrets/credentials.md 內的 NOTICE_LINE_CHANNEL_ACCESS_TOKEN="..."
  4. ~/.secrets/credentials-notes.md 內同上寫法（2026-09-10 瘦身後實際位置）

CEREAL DEV bot（@108nptdd，Hermes／大吉大利智囊團）
  1. 環境變數 CEREAL_DEV_LINE_CHANNEL_ACCESS_TOKEN
  2. ~/.secrets/usage.env 內的 CEREAL_DEV_LINE_CHANNEL_ACCESS_TOKEN=...
  3. ~/.hermes/.env 內的 LINE_CHANNEL_ACCESS_TOKEN=...
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

BOT_INFO_URL = "https://api.line.me/v2/bot/info"
QUOTA_URL = "https://api.line.me/v2/bot/message/quota"
CONSUMPTION_URL = "https://api.line.me/v2/bot/message/quota/consumption"
TOKEN_ENV = "NOTICE_LINE_CHANNEL_ACCESS_TOKEN"
DEV_TOKEN_ENV = "CEREAL_DEV_LINE_CHANNEL_ACCESS_TOKEN"
ENV_FILE = Path(os.path.expanduser("~/.secrets/usage.env"))
CREDENTIALS_FILE = Path(os.path.expanduser("~/.secrets/credentials.md"))
# 2026-09-10 憑證檔瘦身後，Notice token 落在 notes 檔（仍是 KEY=... 舊格式）
CREDENTIALS_NOTES_FILE = Path(os.path.expanduser("~/.secrets/credentials-notes.md"))
HERMES_ENV_FILE = Path(os.path.expanduser("~/.hermes/.env"))

BOT_NOTICE = "notice"
BOT_DEV = "dev"
# basicId 用來防 token 放錯位置時，把別隻 bot 的用量顯示在這一列
BOT_BASIC_IDS = {BOT_NOTICE: "@495vypyi", BOT_DEV: "@108nptdd"}
BOT_NAMES = {BOT_NOTICE: "Notice bot", BOT_DEV: "CEREAL DEV"}

CACHE_TTL_SECONDS = 600.0
REQUEST_TIMEOUT = 6.0

_lock = threading.Lock()
_cache: dict[str, tuple[float, LineQuotaResult]] = {}


@dataclass(slots=True)
class LineQuotaResult:
    available: bool
    used: int = 0
    limit: int | None = None
    percent: float | None = None
    resets_at: float | None = None
    message: str = ""


def _token_from_env_file(path: Path, key: str = TOKEN_ENV) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(rf'^{key}\s*=\s*["\']?([^"\'\n]+)["\']?\s*$', text, re.MULTILINE)
    return match.group(1).strip() if match else None


def _token_from_credentials(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    # 容許 KEY="..."、KEY=...、KEY=`...` 三種寫法；LINE long-lived token 約 172 字元
    match = re.search(rf'{TOKEN_ENV}\s*=\s*["\'`]?([A-Za-z0-9+/=]{{100,}})', text)
    return match.group(1).strip() if match else None


def _token_from_environ(key: str) -> str | None:
    token = os.environ.get(key)
    return token.strip() if token and token.strip() else None


def load_token(bot: str = BOT_NOTICE) -> str | None:
    if bot == BOT_DEV:
        return (
            _token_from_environ(DEV_TOKEN_ENV)
            or _token_from_env_file(ENV_FILE, DEV_TOKEN_ENV)
            or _token_from_env_file(HERMES_ENV_FILE, "LINE_CHANNEL_ACCESS_TOKEN")
        )
    return (
        _token_from_environ(TOKEN_ENV)
        or _token_from_env_file(ENV_FILE)
        or _token_from_credentials(CREDENTIALS_FILE)
        or _token_from_credentials(CREDENTIALS_NOTES_FILE)
    )


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


def fetch(bot: str = BOT_NOTICE, *, mock: bool = False, force: bool = False) -> LineQuotaResult:
    """回傳指定 bot 的本月配額用量；失敗時 available=False，UI 顯示 --。"""
    if mock:
        used = 137 if bot == BOT_NOTICE else 42
        return LineQuotaResult(
            available=True,
            used=used,
            limit=200,
            percent=used / 2,
            resets_at=next_month_reset(),
            message="已同步",
        )

    now = time.time()
    with _lock:
        cached = _cache.get(bot)
        if not force and cached is not None and (now - cached[0]) < CACHE_TTL_SECONDS:
            return cached[1]

    name = BOT_NAMES.get(bot, bot)
    token = load_token(bot)
    if not token:
        result = LineQuotaResult(available=False, message=f"找不到 {name} token")
    else:
        try:
            info = _get_json(BOT_INFO_URL, token)
            if info.get("basicId") != BOT_BASIC_IDS.get(bot):
                result = LineQuotaResult(available=False, message=f"token 不是 {name}")
            else:
                quota = _get_json(QUOTA_URL, token)
                consumption = _get_json(CONSUMPTION_URL, token)
                result = _build_result(quota, consumption)
        except urllib.error.HTTPError as exc:
            result = LineQuotaResult(available=False, message=f"LINE API {exc.code}")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            result = LineQuotaResult(available=False, message=f"LINE 連線失敗（{type(exc).__name__}）")

    with _lock:
        _cache[bot] = (time.time(), result)
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
