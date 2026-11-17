"""直接讀 Claude 帳號的方案用量（含 Fable 5 的每週額度）。

statusLine hook 寫的 `~/.claude/usag-status.json` 只有兩條限制
（five_hour / seven_day），拿不到依模型細分的週額度。這支改打官方
用量 endpoint，一次拿齊：

  GET https://api.anthropic.com/api/oauth/usage
    → five_hour / seven_day 兩條總額度
    → limits[] 內 kind="weekly_scoped" 的依模型週額度（Fable 就在這）

Token 從 macOS Keychain 的 "Claude Code-credentials" 唯讀取用。

⚠️ 刻意「只讀 accessToken、絕不用 refreshToken 換新 token」：
refresh token 是一次性輪替的，若這支程式自己去刷新，會把 Claude Code
手上的憑證換掉，造成兩邊互搶、靜默登出。accessToken 過期就回 None，
交給呼叫端 fallback 讀本地 status 檔；Claude Code 下次啟動會自己續期。
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
KEYCHAIN_SERVICE = "Claude Code-credentials"

# 想完全不碰 Keychain / 不連 API，設 USAG_CLAUDE_API=0（會退回只讀本地 status 檔）
ENABLE_ENV = "USAG_CLAUDE_API"

CACHE_TTL_SECONDS = 120.0
# 按「立即更新」也至少隔這麼久才真的再打一次，避免被 429
FORCE_MIN_INTERVAL_SECONDS = 20.0
# API 暫時失敗（429／斷網）時，這段時間內沿用上一次成功的數字
STALE_OK_SECONDS = 30 * 60.0
REQUEST_TIMEOUT = 6.0

# 名稱可能回 "Fable" 或 "Fable 5"，一律用小寫前綴比對
FABLE_KEY = "fable"

_lock = threading.Lock()
_cache: tuple[float, ClaudeApiUsage] | None = None


@dataclass(slots=True)
class LimitRow:
    percent: float | None = None
    resets_at: float | None = None


@dataclass(slots=True)
class ClaudeApiUsage:
    five_hour: LimitRow | None = None
    seven_day: LimitRow | None = None
    # display_name 小寫 → 該模型的週額度，例如 {"fable": LimitRow(19.0, ...)}
    weekly_scoped: dict[str, LimitRow] = field(default_factory=dict)
    fetched_at: float = 0.0

    def scoped(self, name_prefix: str) -> LimitRow | None:
        prefix = name_prefix.lower()
        for name, row in self.weekly_scoped.items():
            if name.startswith(prefix):
                return row
        return None


def _keychain_credentials() -> dict | None:
    try:
        raw = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if raw.returncode != 0 or not raw.stdout.strip():
        return None
    try:
        data = json.loads(raw.stdout)
    except json.JSONDecodeError:
        return None
    oauth = data.get("claudeAiOauth")
    return oauth if isinstance(oauth, dict) else None


def _access_token() -> str | None:
    """回傳仍在有效期內的 accessToken；過期或抓不到都回 None（不做 refresh）。"""
    oauth = _keychain_credentials()
    if not oauth:
        return None
    token = oauth.get("accessToken")
    if not isinstance(token, str) or not token:
        return None
    expires_at = oauth.get("expiresAt")
    try:
        # expiresAt 是毫秒；留 60 秒安全邊界
        if expires_at is not None and float(expires_at) / 1000.0 <= time.time() + 60:
            return None
    except (TypeError, ValueError):
        return None
    return token


def _parse_ts(value: object) -> float | None:
    """把 ISO 8601（含時區）或 epoch 秒轉成 epoch 秒。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _pct(value: object) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, min(100.0, float(value)))
    except (TypeError, ValueError):
        return None


def _row_from_block(block: object) -> LimitRow | None:
    """解析 top-level 的 five_hour / seven_day 區塊（utilization 已是百分比）。"""
    if not isinstance(block, dict):
        return None
    percent = _pct(block.get("utilization"))
    if percent is None:
        return None
    return LimitRow(percent=percent, resets_at=_parse_ts(block.get("resets_at")))


def _parse(payload: dict) -> ClaudeApiUsage:
    usage = ClaudeApiUsage(fetched_at=time.time())
    usage.five_hour = _row_from_block(payload.get("five_hour"))
    usage.seven_day = _row_from_block(payload.get("seven_day"))

    limits = payload.get("limits")
    if isinstance(limits, list):
        for item in limits:
            if not isinstance(item, dict):
                continue
            kind = item.get("kind")
            percent = _pct(item.get("percent"))
            resets_at = _parse_ts(item.get("resets_at"))
            if percent is None:
                continue
            row = LimitRow(percent=percent, resets_at=resets_at)
            if kind == "session" and usage.five_hour is None:
                usage.five_hour = row
            elif kind == "weekly_all" and usage.seven_day is None:
                usage.seven_day = row
            elif kind == "weekly_scoped":
                scope = item.get("scope") or {}
                model = scope.get("model") if isinstance(scope, dict) else None
                name = (model or {}).get("display_name") if isinstance(model, dict) else None
                if isinstance(name, str) and name.strip():
                    usage.weekly_scoped[name.strip().lower()] = row
    return usage


def _mock_usage() -> ClaudeApiUsage:
    now = time.time()
    return ClaudeApiUsage(
        five_hour=LimitRow(11.0, now + 82 * 60),
        seven_day=LimitRow(19.0, now + ((4 * 24) + 12) * 3600),
        weekly_scoped={"fable": LimitRow(19.0, now + ((4 * 24) + 12) * 3600)},
        fetched_at=now,
    )


def is_enabled() -> bool:
    return os.environ.get(ENABLE_ENV, "1").strip().lower() not in {"0", "false", "no", "off"}


def _stale_cache() -> ClaudeApiUsage | None:
    """這次抓不到時，回傳還不算太舊的上一次成功結果。"""
    with _lock:
        if _cache is not None and (time.time() - _cache[0]) < STALE_OK_SECONDS:
            return _cache[1]
    return None


def fetch(*, mock: bool = False, force: bool = False) -> ClaudeApiUsage | None:
    """回傳最新用量；抓不到（關閉 / 沒 token / token 過期 / 網路錯）一律回 None。"""
    if mock:
        return _mock_usage()

    if not is_enabled():
        return None

    global _cache
    now = time.time()
    with _lock:
        if _cache is not None:
            age = now - _cache[0]
            if age < (FORCE_MIN_INTERVAL_SECONDS if force else CACHE_TTL_SECONDS):
                return _cache[1]

    token = _access_token()
    if token is None:
        return _stale_cache()

    request = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "usage-menubar",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError):
        return _stale_cache()

    if not isinstance(payload, dict):
        return _stale_cache()

    usage = _parse(payload)
    if usage.five_hour is None and usage.seven_day is None and not usage.weekly_scoped:
        return None

    with _lock:
        _cache = (time.time(), usage)
    return usage
