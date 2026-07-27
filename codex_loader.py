from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import subprocess
import threading
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from history_loader import UsageEntry

logger = logging.getLogger(__name__)

SESSIONS_DIR = Path(os.path.expanduser("~/.codex/sessions"))
STATE_DB = Path(os.path.expanduser("~/.codex/state_5.sqlite"))

# Stable, launchd-safe locations to find the codex binary. The bundled app path
# is preferred because the menubar runs under launchd with a minimal PATH that
# does not include nvm's shims.
CODEX_BINARY_CANDIDATES = (
    "/Applications/Codex.app/Contents/Resources/codex",
    os.path.expanduser("~/.nvm/versions/node/v22.14.0/bin/codex"),
    "codex",
)


@dataclass(slots=True)
class CodexRateLimits:
    five_hour_pct: float | None
    five_hour_resets_at: float | None
    seven_day_pct: float | None
    seven_day_resets_at: float | None
    updated_at: str = ""


def load_entries(hours_back: int = 0) -> list[UsageEntry]:
    if not SESSIONS_DIR.is_dir():
        return []

    entries: list[UsageEntry] = []
    seen: set[str] = set()
    cutoff = datetime.now(UTC) - timedelta(hours=hours_back) if hours_back > 0 else None
    cutoff_ts = cutoff.timestamp() if cutoff else None
    models = _load_thread_models()

    for jsonl_path in SESSIONS_DIR.rglob("*.jsonl"):
        if cutoff_ts is not None:
            try:
                if jsonl_path.stat().st_mtime < cutoff_ts:
                    continue
            except OSError as exc:
                logger.warning("failed to stat session log %s: %s", jsonl_path, exc)
                continue
        entry = _parse_jsonl(jsonl_path, models, cutoff)
        if entry is None or entry.session_id in seen:
            continue
        seen.add(entry.session_id)
        entries.append(entry)

    entries.sort(key=lambda entry: entry.timestamp)
    return entries


def load_rate_limits() -> CodexRateLimits | None:
    if not SESSIONS_DIR.is_dir():
        return None
    for path in _recent_jsonl_files():
        rate_limits = _extract_rate_limits(path)
        if rate_limits is not None:
            return rate_limits
    return None


def load_rate_limits_best(timeout: float = 8.0) -> CodexRateLimits | None:
    """Return live rate limits from the Codex app-server, falling back to the
    (possibly stale) snapshot parsed from session rollout files."""
    live = load_rate_limits_live(timeout=timeout)
    if live is not None:
        return live
    return load_rate_limits()


def load_rate_limits_live(timeout: float = 8.0) -> CodexRateLimits | None:
    """Query the Codex app-server over JSON-RPC for the current account rate
    limits. This is the same source the Codex TUI uses, so it reflects real
    usage immediately instead of lagging behind sparse rollout checkpoints."""
    binary = _codex_binary()
    if binary is None:
        return None
    try:
        proc = subprocess.Popen(
            [binary, "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        logger.warning("failed to spawn codex app-server: %s", exc)
        return None

    captured: dict[str, Any] = {}

    def _reader() -> None:
        stdout = proc.stdout
        if stdout is None:
            return
        for line in stdout:
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == 2:
                captured["result"] = _as_dict(msg.get("result"))
                break

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()

    try:
        stdin = proc.stdin
        if stdin is None:
            return None
        for message in (
            {
                "id": 1,
                "method": "initialize",
                "params": {"clientInfo": {"name": "usag", "version": "0.1.0"}},
            },
            {"method": "initialized"},
            {"id": 2, "method": "account/rateLimits/read", "params": {}},
        ):
            stdin.write(json.dumps(message) + "\n")
            stdin.flush()
    except (OSError, ValueError) as exc:
        logger.warning("failed to talk to codex app-server: %s", exc)
        _terminate(proc)
        return None

    reader.join(timeout)
    _terminate(proc)

    result = captured.get("result")
    if result is None:
        return None
    return _parse_live_rate_limits(result)


def _codex_binary() -> str | None:
    for candidate in CODEX_BINARY_CANDIDATES:
        if os.sep in candidate:
            if os.path.exists(candidate):
                return candidate
        else:
            resolved = shutil.which(candidate)
            if resolved:
                return resolved
    return None


def _terminate(proc: subprocess.Popen[str]) -> None:
    try:
        proc.terminate()
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        with suppress(OSError):
            proc.kill()
    except OSError:
        pass


def _parse_live_rate_limits(result: dict[str, Any]) -> CodexRateLimits | None:
    rate_limits = _as_dict(result.get("rateLimits"))
    if not rate_limits:
        return None
    primary = _as_dict(rate_limits.get("primary"))
    secondary = _as_dict(rate_limits.get("secondary"))
    five_pct = _as_optional_float(primary.get("usedPercent"))
    five_reset = _as_optional_float(primary.get("resetsAt"))
    seven_pct = _as_optional_float(secondary.get("usedPercent"))
    seven_reset = _as_optional_float(secondary.get("resetsAt"))
    if five_pct is None and seven_pct is None:
        return None
    # Unlike the rollout-file path, the live value is authoritative even when a
    # window is at its reset boundary, so we never force it to zero here.
    return CodexRateLimits(
        five_hour_pct=five_pct,
        five_hour_resets_at=five_reset,
        seven_day_pct=seven_pct,
        seven_day_resets_at=seven_reset,
        updated_at=datetime.now(UTC).isoformat(),
    )


def _load_thread_models() -> dict[str, str]:
    if not STATE_DB.exists():
        return {}
    try:
        with sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True) as conn:
            rows = conn.execute(
                "SELECT id, model FROM threads WHERE model IS NOT NULL",
            ).fetchall()
    except (OSError, sqlite3.Error):
        return {}
    return {
        thread_id: model
        for thread_id, model in rows
        if isinstance(thread_id, str) and isinstance(model, str) and model
    }


def _recent_jsonl_files() -> list[Path]:
    paths = list(SESSIONS_DIR.rglob("*.jsonl"))
    try:
        return sorted(paths, key=lambda path: path.stat().st_mtime, reverse=True)[:5]
    except OSError as exc:
        logger.warning("failed to list codex session dirs: %s", exc)
        return []


def _extract_rate_limits(path: Path) -> CodexRateLimits | None:
    last_rate_limits: tuple[dict[str, Any], str] | None = None
    try:
        with path.open(encoding="utf-8") as file:
            for line in file:
                data = _load_json_line(line)
                if data is None or data.get("type") != "event_msg":
                    continue
                payload = _as_dict(data.get("payload"))
                if payload.get("type") != "token_count":
                    continue
                rate_limits = _as_dict(payload.get("rate_limits"))
                if rate_limits:
                    last_rate_limits = (rate_limits, _as_str(data.get("timestamp")))
    except OSError as exc:
        logger.warning("failed to read codex session %s: %s", path, exc)
        return None
    if last_rate_limits is None:
        return None
    rate_limits, updated_at = last_rate_limits
    primary = _as_dict(rate_limits.get("primary"))
    secondary = _as_dict(rate_limits.get("secondary"))
    five_pct = _as_optional_float(primary.get("used_percent"))
    five_reset = _as_optional_float(primary.get("resets_at"))
    seven_pct = _as_optional_float(secondary.get("used_percent"))
    seven_reset = _as_optional_float(secondary.get("resets_at"))
    now_ts = datetime.now(UTC).timestamp()
    if five_reset is not None and five_reset < now_ts:
        five_pct = 0.0
    if seven_reset is not None and seven_reset < now_ts:
        seven_pct = 0.0
    if five_pct is None and seven_pct is None:
        return None
    return CodexRateLimits(
        five_hour_pct=five_pct,
        five_hour_resets_at=five_reset,
        seven_day_pct=seven_pct,
        seven_day_resets_at=seven_reset,
        updated_at=updated_at,
    )


def _parse_jsonl(path: Path, models: dict[str, str], cutoff: datetime | None) -> UsageEntry | None:
    session_id = ""
    session_timestamp = ""
    project = "unknown"
    last_usage: dict[str, Any] | None = None
    try:
        with path.open(encoding="utf-8") as file:
            for line in file:
                data = _load_json_line(line)
                if data is None:
                    continue
                if data.get("type") == "session_meta":
                    payload = _as_dict(data.get("payload"))
                    session_id = _as_str(payload.get("id"))
                    session_timestamp = _as_str(payload.get("timestamp"))
                    project = _project_from_cwd(_as_str(payload.get("cwd")))
                    continue
                if data.get("type") != "event_msg":
                    continue
                payload = _as_dict(data.get("payload"))
                if payload.get("type") != "token_count":
                    continue
                usage = _as_dict(_as_dict(payload.get("info")).get("total_token_usage"))
                if usage:
                    last_usage = usage
    except OSError as exc:
        logger.warning("failed to parse codex session %s: %s", path, exc)
        return None
    timestamp = _parse_timestamp(session_timestamp)
    if not session_id or last_usage is None or timestamp is None:
        return None
    if cutoff is not None and timestamp < cutoff:
        return None
    cached = _as_int(last_usage.get("cached_input_tokens"))
    input_tokens = max(0, _as_int(last_usage.get("input_tokens")) - cached)
    output_tokens = _as_int(last_usage.get("output_tokens")) + _as_int(
        last_usage.get("reasoning_output_tokens"),
    )
    if input_tokens == 0 and output_tokens == 0:
        return None
    return UsageEntry(
        timestamp=timestamp,
        session_id=session_id,
        message_id=session_id,
        request_id="",
        model=models.get(session_id, "unknown"),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_tokens=0,
        cache_read_tokens=cached,
        cost_usd=None,
        project=project,
    )


def _load_json_line(line: str) -> dict[str, Any] | None:
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _parse_timestamp(value: str) -> datetime | None:
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=UTC)
    return timestamp.astimezone(UTC)


def _project_from_cwd(cwd: str) -> str:
    return Path(os.path.expanduser(cwd)).name if cwd else "unknown"


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_int(value: Any) -> int:
    return value if isinstance(value, int) else 0


def _as_str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _as_optional_float(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) else None
