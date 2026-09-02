"""每日暖機：在指定時間跑一次最便宜模型的請求，把 5 小時額度視窗提前起算。

Claude Code 的 5 小時視窗是「從當天第一句話開始算」的滾動視窗。早上先講一句 hi，
中午開工時那個視窗已經快到期、額度幾乎沒動，等於多拿到一段滿額度的時間。

跑的是 `claude -p hi --model haiku`（最便宜的模型、關掉 MCP、空目錄），
一次約 2 萬 token、不到 US$0.02 的權重，對 5 小時額度可以忽略。
"""

from __future__ import annotations

import datetime as _dt
import glob
import json
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

LOGGER = logging.getLogger(__name__)

CONFIG_PATH = Path(os.path.expanduser("~/.claude/usag_warmup.json"))

DEFAULT_TIME = "07:30"
DEFAULT_MODEL = "haiku"
RUN_TIMEOUT_SECONDS = 180.0

# 錯過排定時間太久就不補跑了（例如電腦整天沒開，晚上才喚醒）。
# 補跑窗口設 6 小時：07:30 排程最晚 13:30 前喚醒都還補得到。
CATCHUP_WINDOW_SECONDS = 6 * 3600


@dataclass(frozen=True)
class WarmupConfig:
    enabled: bool = False
    time: str = DEFAULT_TIME
    model: str = DEFAULT_MODEL
    claude_bin: str = ""
    last_run_date: str = ""
    last_run_at: float = 0.0
    last_result: str = ""


def _parse_hhmm(value: str) -> tuple[int, int]:
    try:
        hh, mm = value.split(":", 1)
        hour, minute = int(hh), int(mm)
    except (ValueError, AttributeError):
        return 7, 30
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return 7, 30
    return hour, minute


def load() -> WarmupConfig:
    if not CONFIG_PATH.exists():
        return WarmupConfig()
    try:
        with CONFIG_PATH.open(encoding="utf-8") as file:
            raw = json.load(file)
    except (OSError, json.JSONDecodeError):
        LOGGER.warning("warmup config 讀不起來，用預設值")
        return WarmupConfig()
    if not isinstance(raw, dict):
        return WarmupConfig()
    return WarmupConfig(
        enabled=bool(raw.get("enabled", False)),
        time=str(raw.get("time", DEFAULT_TIME)),
        model=str(raw.get("model", DEFAULT_MODEL)),
        claude_bin=str(raw.get("claude_bin", "")),
        last_run_date=str(raw.get("last_run_date", "")),
        last_run_at=float(raw.get("last_run_at", 0.0) or 0.0),
        last_result=str(raw.get("last_result", "")),
    )


def save(config: WarmupConfig) -> None:
    payload = {
        "enabled": config.enabled,
        "time": config.time,
        "model": config.model,
        "claude_bin": config.claude_bin,
        "last_run_date": config.last_run_date,
        "last_run_at": config.last_run_at,
        "last_result": config.last_result,
    }
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)
    except OSError:
        LOGGER.warning("warmup config 寫不進去：%s", CONFIG_PATH)


def find_claude_binary(configured: str = "") -> str:
    """找 claude 執行檔。app 是 GUI 啟動的，PATH 不含 nvm，所以要自己找。"""
    if configured and os.access(configured, os.X_OK):
        return configured

    found = shutil.which("claude")
    if found:
        return found

    candidates = [
        os.path.expanduser("~/.claude/local/claude"),
        os.path.expanduser("~/.local/bin/claude"),
        "/opt/homebrew/bin/claude",
        "/usr/local/bin/claude",
    ]
    # nvm 會有多個 node 版本，挑版號最新的那個
    nvm = sorted(glob.glob(os.path.expanduser("~/.nvm/versions/node/*/bin/claude")))
    candidates.extend(reversed(nvm))

    for path in candidates:
        if os.access(path, os.X_OK):
            return path
    return ""


def scheduled_epoch(config: WarmupConfig, now: float) -> float:
    """今天排定的暖機時刻（epoch 秒）。"""
    hour, minute = _parse_hhmm(config.time)
    today = _dt.datetime.fromtimestamp(now).replace(
        hour=hour, minute=minute, second=0, microsecond=0,
    )
    return today.timestamp()


def should_run(config: WarmupConfig, now: float) -> bool:
    if not config.enabled:
        return False
    today = _dt.datetime.fromtimestamp(now).strftime("%Y-%m-%d")
    if config.last_run_date == today:
        return False
    due = scheduled_epoch(config, now)
    if now < due:
        return False
    # 睡了一整天才喚醒的話，補跑反而會把視窗開在奇怪的時間，寧可跳過等明天
    return (now - due) <= CATCHUP_WINDOW_SECONDS


def run_once(config: WarmupConfig) -> WarmupConfig:
    """真的送一句 hi 出去。回傳更新過（且已存檔）的設定。"""
    now = _dt.datetime.now()
    binary = find_claude_binary(config.claude_bin)
    if not binary:
        result = "找不到 claude 指令"
    else:
        env = {
            k: v
            for k, v in os.environ.items()
            # py2app 會注入 PYTHON* 變數，帶進子行程會干擾別人
            if not k.startswith(("PYTHON", "__PYVENV"))
        }
        env["PATH"] = os.path.dirname(binary) + os.pathsep + env.get("PATH", "/usr/bin:/bin")
        try:
            with tempfile.TemporaryDirectory(prefix="usag-warmup-") as workdir:
                proc = subprocess.run(  # noqa: S603
                    [
                        binary,
                        "-p",
                        "hi",
                        "--model",
                        config.model or DEFAULT_MODEL,
                        "--output-format",
                        "json",
                        "--strict-mcp-config",
                        "--mcp-config",
                        '{"mcpServers":{}}',
                    ],
                    cwd=workdir,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=RUN_TIMEOUT_SECONDS,
                    check=False,
                )
            result = "成功" if proc.returncode == 0 else f"失敗（exit {proc.returncode}）"
            if proc.returncode != 0:
                LOGGER.warning("warmup 失敗：%s", (proc.stderr or "")[:400])
        except subprocess.TimeoutExpired:
            result = "逾時"
        except OSError as exc:
            result = f"錯誤：{exc.__class__.__name__}"

    updated = replace(
        config,
        claude_bin=binary or config.claude_bin,
        last_run_date=now.strftime("%Y-%m-%d") if result == "成功" else config.last_run_date,
        last_run_at=now.timestamp(),
        last_result=result,
    )
    save(updated)
    return updated


def row_texts(config: WarmupConfig, now: float) -> tuple[str, str]:
    """給選單那一列用的（標題, 右側狀態）。分兩欄是因為單欄會被擠掉尾字。"""
    title = f"每日暖機 · {config.time}"
    if not config.enabled:
        return title, "已關閉"
    today = _dt.datetime.fromtimestamp(now).strftime("%Y-%m-%d")
    if config.last_run_date == today:
        stamp = _dt.datetime.fromtimestamp(config.last_run_at).strftime("%H:%M")
        return title, f"{stamp} ✓"
    if config.last_result and config.last_result != "成功":
        brief = config.last_result.split("（", 1)[0]
        return title, f"上次{brief}"
    return title, "等候中"
