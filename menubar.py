# mypy: disable-error-code="import-untyped,misc"
# PyObjC modules do not ship type stubs, and their base classes resolve to Any in mypy.
from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import objc
from AppKit import (
    NSApp,
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSBezierPath,
    NSColor,
    NSFont,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSImage,
    NSKernAttributeName,
    NSMakeRect,
    NSMakeSize,
    NSMenu,
    NSMenuItem,
    NSStatusBar,
    NSTextAlignmentRight,
    NSTextField,
    NSTrackingActiveAlways,
    NSTrackingArea,
    NSTrackingInVisibleRect,
    NSTrackingMouseEnteredAndExited,
    NSVariableStatusItemLength,
    NSView,
)
from AppKit import NSEventTrackingRunLoopMode, NSImageScaleProportionallyUpOrDown, NSImageView
from Foundation import (
    NSAttributedString,
    NSBundle,
    NSDefaultRunLoopMode,
    NSObject,
    NSRunLoop,
    NSRunLoopCommonModes,
    NSTimer,
)

import claude_api_loader
import codex_loader
import line_quota
import warmup
from history_loader import load_entries
from pricing import calculate_cost
from usage_client import ClaudeUsageClient, PollOutcome, PollState
from usage_rate import GROUP_NAMES, UsageRateTracker

MENU_WIDTH = 304.0
ROW_INSET = 12.0
TRACK_HEIGHT = 4.0

HEADER_ROW_HEIGHT = 34.0
SECTION_ROW_HEIGHT = 22.0
QUOTA_ROW_HEIGHT = 40.0
INFO_ROW_HEIGHT = 19.0
ACTION_ROW_HEIGHT = 26.0

WARN_PERCENT = 80.0
DANGER_PERCENT = 95.0

LINE_ROW_TITLES = {line_quota.BOT_NOTICE: "Notice Bot", line_quota.BOT_DEV: "CEREAL DEV"}

STATUS_FILE = os.path.expanduser("~/.claude/usag-status.json")


def _resolve_resource(name: str) -> str:
    bundle = NSBundle.mainBundle()
    if bundle is not None:
        stem, _, ext = name.rpartition(".")
        path = bundle.pathForResource_ofType_(stem, ext)
        if path:
            return str(path)
    return str(Path(__file__).parent / "assets" / name)


CLAUDE_ICON_PATH = _resolve_resource("claude.webp")
CODEX_ICON_PATH = _resolve_resource("codex.webp")

_APP_DELEGATE: AppDelegate | None = None


@dataclass(slots=True)
class QuotaRowState:
    title: str
    percent: float | None
    meta_text: str
    available: bool = True


@dataclass(slots=True)
class MenuState:
    claude_session: QuotaRowState
    claude_weekly: QuotaRowState
    claude_fable: QuotaRowState
    codex_session: QuotaRowState
    codex_weekly: QuotaRowState
    line_month: QuotaRowState
    line_dev_month: QuotaRowState
    version_text: str
    today_text: str
    status_text: str
    updated_at: float | None = None


def format_human_time(seconds: float) -> str:
    if seconds <= 0:
        return "0m"
    days, remainder = divmod(int(seconds), 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)

    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def format_updated_ago(updated_at: float | None) -> str:
    if updated_at is None:
        return "尚未更新"
    delta = max(0.0, time.time() - updated_at)
    if delta < 60:
        return "剛剛更新"
    minutes = int(delta // 60)
    if minutes < 60:
        return f"{minutes} 分鐘前更新"
    hours = int(minutes // 60)
    if hours < 24:
        return f"{hours} 小時前更新"
    return f"{int(hours // 24)} 天前更新"


class ProgressBarView(NSView):
    percent = objc.ivar()
    available = objc.ivar()

    def initWithFrame_(self, frame: Any) -> ProgressBarView:
        self = objc.super(ProgressBarView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.percent = None
        self.available = False
        return self

    def isFlipped(self) -> bool:
        return True

    def setPercent_available_(self, percent: float | None, available: bool) -> None:
        self.percent = percent
        self.available = available
        self.setNeedsDisplay_(True)

    def drawRect_(self, dirty_rect: Any) -> None:
        bounds = self.bounds()
        rect = NSMakeRect(
            0,
            (bounds.size.height - TRACK_HEIGHT) / 2,
            bounds.size.width,
            TRACK_HEIGHT,
        )
        _track_color().setFill()
        _fill_rounded_rect(rect, TRACK_HEIGHT / 2)

        if not self.available or self.percent is None:
            return

        pct = max(0.0, min(100.0, float(self.percent)))
        if pct <= 0:
            return
        fill_width = min(bounds.size.width, max(3.0, bounds.size.width * pct / 100.0))
        fill_rect = NSMakeRect(rect.origin.x, rect.origin.y, fill_width, rect.size.height)
        _bar_color(pct).setFill()
        _fill_rounded_rect(fill_rect, TRACK_HEIGHT / 2)


class MenuHeaderView(NSView):
    icon_view = objc.ivar()
    title_label = objc.ivar()
    version_label = objc.ivar()
    dot_color = objc.ivar()

    def initWithTitle_iconPath_(self, title: str, icon_path: str) -> MenuHeaderView:
        self = objc.super(MenuHeaderView, self).initWithFrame_(
            NSMakeRect(0, 0, MENU_WIDTH, HEADER_ROW_HEIGHT),
        )
        if self is None:
            return None
        self.dot_color = None
        self.icon_view = _image_view(icon_path)
        self.title_label = _label(title, NSFont.systemFontOfSize_weight_(13.5, 0.3), _label_color())
        self.version_label = _label(
            "",
            NSFont.systemFontOfSize_weight_(11, 0.0),
            _secondary_color(),
            NSTextAlignmentRight,
        )
        for view in (self.icon_view, self.title_label, self.version_label):
            if view is not None:
                self.addSubview_(view)
        self.relayout()
        return self

    def isFlipped(self) -> bool:
        return True

    def layout(self) -> None:
        objc.super(MenuHeaderView, self).layout()
        self.relayout()

    @objc.python_method
    def relayout(self) -> None:
        width = self.bounds().size.width
        text_x = ROW_INSET
        if self.icon_view is not None:
            self.icon_view.setFrame_(NSMakeRect(ROW_INSET, 9, 17, 17))
            text_x = ROW_INSET + 24
        elif self.dot_color is not None:
            text_x = ROW_INSET + 24
        self.title_label.setFrame_(NSMakeRect(text_x, 9, (width * 0.58) - text_x, 18))
        self.version_label.setFrame_(
            NSMakeRect(width * 0.58, 10, width - (width * 0.58) - ROW_INSET, 16),
        )

    def setVersionText_(self, text: str) -> None:
        self.version_label.setStringValue_(text)

    def setDotColor_(self, color: Any) -> None:
        self.dot_color = color
        self.relayout()
        self.setNeedsDisplay_(True)

    def drawRect_(self, dirty_rect: Any) -> None:
        if self.dot_color is None:
            return
        self.dot_color.setFill()
        NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(ROW_INSET + 4, 13, 10, 10)).fill()


class SectionHeaderView(NSView):
    title_label = objc.ivar()

    def initWithTitle_(self, title: str) -> SectionHeaderView:
        self = objc.super(SectionHeaderView, self).initWithFrame_(
            NSMakeRect(0, 0, MENU_WIDTH, SECTION_ROW_HEIGHT),
        )
        if self is None:
            return None
        self.title_label = _label("", NSFont.systemFontOfSize_weight_(10.5, 0.4), _tertiary_color())
        self.title_label.setAttributedStringValue_(
            NSAttributedString.alloc().initWithString_attributes_(
                title,
                {
                    NSFontAttributeName: NSFont.systemFontOfSize_weight_(10.5, 0.4),
                    NSForegroundColorAttributeName: _tertiary_color(),
                    NSKernAttributeName: 0.6,
                },
            ),
        )
        self.addSubview_(self.title_label)
        self.relayout()
        return self

    def isFlipped(self) -> bool:
        return True

    def layout(self) -> None:
        objc.super(SectionHeaderView, self).layout()
        self.relayout()

    @objc.python_method
    def relayout(self) -> None:
        width = self.bounds().size.width
        self.title_label.setFrame_(NSMakeRect(ROW_INSET, 6, width - (ROW_INSET * 2), 14))


class QuotaRowView(NSView):
    title_label = objc.ivar()
    meta_label = objc.ivar()
    progress_bar = objc.ivar()

    def initWithFrame_(self, frame: Any) -> QuotaRowView:
        self = objc.super(QuotaRowView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.title_label = _label("", NSFont.systemFontOfSize_weight_(12.5, 0.0), _label_color())
        self.meta_label = _label(
            "",
            NSFont.systemFontOfSize_weight_(11.5, 0.0),
            _secondary_color(),
            NSTextAlignmentRight,
        )
        self.progress_bar = ProgressBarView.alloc().initWithFrame_(
            NSMakeRect(0, 0, 1, TRACK_HEIGHT),
        )
        for view in (self.title_label, self.meta_label, self.progress_bar):
            self.addSubview_(view)
        self.relayout()
        return self

    def isFlipped(self) -> bool:
        return True

    def layout(self) -> None:
        objc.super(QuotaRowView, self).layout()
        self.relayout()

    @objc.python_method
    def relayout(self) -> None:
        width = self.bounds().size.width
        content_width = width - (ROW_INSET * 2)
        self.title_label.setFrame_(NSMakeRect(ROW_INSET, 3, content_width * 0.40, 17))
        self.meta_label.setFrame_(
            NSMakeRect(ROW_INSET + (content_width * 0.40), 3, content_width * 0.60, 17),
        )
        self.progress_bar.setFrame_(NSMakeRect(ROW_INSET, 26, content_width, TRACK_HEIGHT))

    def setRowState_(self, row: QuotaRowState) -> None:
        self.title_label.setStringValue_(row.title)
        self.meta_label.setStringValue_(row.meta_text)
        self.meta_label.setTextColor_(_meta_color(row))
        self.progress_bar.setPercent_available_(row.percent, row.available)
        self.relayout()


class InfoRowView(NSView):
    title_label = objc.ivar()

    def initWithFrame_(self, frame: Any) -> InfoRowView:
        self = objc.super(InfoRowView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.title_label = _label("", NSFont.systemFontOfSize_weight_(11.5, 0.0), _secondary_color())
        self.addSubview_(self.title_label)
        self.relayout()
        return self

    def isFlipped(self) -> bool:
        return True

    def layout(self) -> None:
        objc.super(InfoRowView, self).layout()
        self.relayout()

    @objc.python_method
    def relayout(self) -> None:
        width = self.bounds().size.width
        self.title_label.setFrame_(NSMakeRect(ROW_INSET, 1, width - (ROW_INSET * 2), 16))

    def setText_(self, text: str) -> None:
        self.title_label.setStringValue_(text)


class ActionRowView(NSView):
    title_label = objc.ivar()
    meta_label = objc.ivar()
    target = objc.ivar()
    action_name = objc.ivar()
    hovering = objc.ivar()
    enabled = objc.ivar()

    def initWithTitle_target_action_(self, title: str, target: Any, action: str) -> ActionRowView:
        self = objc.super(ActionRowView, self).initWithFrame_(
            NSMakeRect(0, 0, MENU_WIDTH, ACTION_ROW_HEIGHT),
        )
        if self is None:
            return None
        self.target = target
        self.action_name = action
        self.hovering = False
        self.enabled = True
        self.title_label = _label(title, NSFont.systemFontOfSize_weight_(13, 0.0), _label_color())
        self.meta_label = _label(
            "",
            NSFont.systemFontOfSize_weight_(11, 0.0),
            _secondary_color(),
            NSTextAlignmentRight,
        )
        for view in (self.title_label, self.meta_label):
            self.addSubview_(view)
        self.relayout()
        return self

    def isFlipped(self) -> bool:
        return True

    def layout(self) -> None:
        objc.super(ActionRowView, self).layout()
        self.relayout()

    @objc.python_method
    def relayout(self) -> None:
        width = self.bounds().size.width
        self.title_label.setFrame_(NSMakeRect(ROW_INSET, 4, width * 0.5, 18))
        self.meta_label.setFrame_(
            NSMakeRect(width * 0.42, 5, width - (width * 0.42) - ROW_INSET, 16),
        )

    def updateTrackingAreas(self) -> None:
        objc.super(ActionRowView, self).updateTrackingAreas()
        for area in self.trackingAreas():
            self.removeTrackingArea_(area)
        area = NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
            self.bounds(),
            NSTrackingMouseEnteredAndExited | NSTrackingActiveAlways | NSTrackingInVisibleRect,
            self,
            None,
        )
        self.addTrackingArea_(area)

    def mouseEntered_(self, event: Any) -> None:
        if not self.enabled:
            return
        self.hovering = True
        self._applyColors()

    def mouseExited_(self, event: Any) -> None:
        self.hovering = False
        self._applyColors()

    def mouseUp_(self, event: Any) -> None:
        if not self.enabled:
            return
        self.hovering = False
        self._applyColors()
        item = self.enclosingMenuItem()
        menu = item.menu() if item is not None else None
        if menu is not None:
            menu.cancelTracking()
        if self.target is not None and self.action_name:
            handler = getattr(self.target, self.action_name, None)
            if handler is not None:
                handler(self)

    @objc.python_method
    def _applyColors(self) -> None:
        if self.hovering:
            self.title_label.setTextColor_(NSColor.selectedMenuItemTextColor())
            self.meta_label.setTextColor_(
                NSColor.selectedMenuItemTextColor().colorWithAlphaComponent_(0.75),
            )
        elif self.enabled:
            self.title_label.setTextColor_(_label_color())
            self.meta_label.setTextColor_(_secondary_color())
        else:
            self.title_label.setTextColor_(_tertiary_color())
            self.meta_label.setTextColor_(_tertiary_color())
        self.setNeedsDisplay_(True)

    def drawRect_(self, dirty_rect: Any) -> None:
        if not self.hovering:
            return
        bounds = self.bounds()
        rect = NSMakeRect(5, 1, bounds.size.width - 10, bounds.size.height - 2)
        NSColor.selectedContentBackgroundColor().setFill()
        _fill_rounded_rect(rect, 5.0)

    def setTitleText_(self, text: str) -> None:
        self.title_label.setStringValue_(text)

    def setMetaText_(self, text: str) -> None:
        self.meta_label.setStringValue_(text)

    def setEnabledState_(self, enabled: bool) -> None:
        self.enabled = enabled
        if not enabled:
            self.hovering = False
        self._applyColors()


class AppDelegate(NSObject):
    status_item = objc.ivar()
    menu = objc.ivar()
    rows = objc.ivar()
    timer = objc.ivar()
    mock = objc.ivar()
    interval = objc.ivar()
    tracker = objc.ivar()
    latest_state = objc.ivar()
    codex_5h_pct = objc.ivar()
    force_line = objc.ivar()
    _refresh_in_flight = objc.ivar()
    refresh_started_at = objc.ivar()
    warmup_config = objc.ivar()
    _warmup_in_flight = objc.ivar()
    _warmup_pending = objc.ivar()

    def initWithMock_interval_(self, mock: bool, interval: int) -> AppDelegate:
        self = objc.super(AppDelegate, self).init()
        if self is None:
            return None
        self.mock = mock
        self.interval = max(30, interval)
        self.tracker = UsageRateTracker(mock=mock)
        self.codex_5h_pct = None
        self.latest_state = _empty_state()
        self.force_line = False
        self._refresh_in_flight = False
        self.refresh_started_at = 0.0
        self.warmup_config = warmup.load()
        warmup.log(
            f"app 啟動：enabled={self.warmup_config.enabled} "
            f"time={self.warmup_config.time} last_run_date={self.warmup_config.last_run_date}",
        )
        self._warmup_in_flight = False
        self._warmup_pending = None
        self.rows = {}
        return self

    def applicationDidFinishLaunching_(self, notification: Any) -> None:
        NSApp.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
        self.status_item = NSStatusBar.systemStatusBar().statusItemWithLength_(
            NSVariableStatusItemLength,
        )
        self.status_item.button().setTitle_("Claude ...")
        self._buildMenu()
        self.status_item.setMenu_(self.menu)

        self._refresh()
        self._maybeWarmup()
        self.timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            self.interval,
            self,
            "timerFired:",
            None,
            True,
        )
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.timer, NSRunLoopCommonModes)

    @objc.python_method
    def _buildMenu(self) -> None:
        menu = NSMenu.alloc().init()
        menu.setDelegate_(self)
        menu.setMinimumWidth_(MENU_WIDTH)
        rows: dict[str, Any] = {}

        rows["header"] = MenuHeaderView.alloc().initWithTitle_iconPath_(
            "Claude Code",
            CLAUDE_ICON_PATH,
        )
        _add_view_item(menu, rows["header"])
        _add_view_item(menu, SectionHeaderView.alloc().initWithTitle_("方案用量上限"))
        rows["claude_session"] = _add_quota_row(menu)
        rows["claude_weekly"] = _add_quota_row(menu)
        rows["claude_fable"] = _add_quota_row(menu)

        menu.addItem_(NSMenuItem.separatorItem())
        codex_header = MenuHeaderView.alloc().initWithTitle_iconPath_("Codex", CODEX_ICON_PATH)
        _add_view_item(menu, codex_header)
        rows["codex_session"] = _add_quota_row(menu)
        rows["codex_weekly"] = _add_quota_row(menu)

        menu.addItem_(NSMenuItem.separatorItem())
        line_header = MenuHeaderView.alloc().initWithTitle_iconPath_("LINE 本月配額", "")
        line_header.setDotColor_(
            NSColor.colorWithCalibratedRed_green_blue_alpha_(0.02, 0.78, 0.33, 1.0),
        )
        _add_view_item(menu, line_header)
        rows["line_header"] = line_header
        rows["line_month"] = _add_quota_row(menu)
        rows["line_dev_month"] = _add_quota_row(menu)

        menu.addItem_(NSMenuItem.separatorItem())
        rows["today"] = _add_info_row(menu)
        rows["status"] = _add_info_row(menu)

        menu.addItem_(NSMenuItem.separatorItem())
        rows["warmup"] = ActionRowView.alloc().initWithTitle_target_action_(
            "每日暖機",
            self,
            "toggleWarmup_",
        )
        _add_view_item(menu, rows["warmup"])

        menu.addItem_(NSMenuItem.separatorItem())
        rows["refresh"] = ActionRowView.alloc().initWithTitle_target_action_(
            "立即更新",
            self,
            "refreshNow_",
        )
        _add_view_item(menu, rows["refresh"])
        rows["quit"] = ActionRowView.alloc().initWithTitle_target_action_(
            "結束",
            self,
            "quitApp_",
        )
        _add_view_item(menu, rows["quit"])

        self.menu = menu
        self.rows = rows

    def menuWillOpen_(self, menu: Any) -> None:
        self._applyState(self.latest_state)
        self._refresh()

    def timerFired_(self, timer: Any) -> None:
        self._refresh()
        self._maybeWarmup()

    def refreshNow_(self, sender: Any) -> None:
        self.force_line = True
        self._refresh()

    def toggleWarmup_(self, sender: Any) -> None:
        if self._warmup_in_flight:
            return
        config = dataclasses.replace(self.warmup_config, enabled=not self.warmup_config.enabled)
        warmup.save(config)
        self.warmup_config = config
        self._applyWarmupRow()
        # 剛打開、而且今天早就過了排定時間，就當場補一次，不用等到明天
        if config.enabled and warmup.should_run(config, time.time()):
            self._startWarmup()

    def warmupFinished_(self, sender: Any) -> None:
        if self._warmup_pending is not None:
            self.warmup_config = self._warmup_pending
            self._warmup_pending = None
        self._warmup_in_flight = False
        self._applyWarmupRow()

    @objc.python_method
    def _maybeWarmup(self) -> None:
        if self._warmup_in_flight:
            if warmup.should_run(self.warmup_config, time.time()):
                warmup.log("該暖機了，但上一次還卡在執行中，跳過")
            return
        if warmup.should_run(self.warmup_config, time.time()):
            self._startWarmup()

    @objc.python_method
    def _startWarmup(self) -> None:
        self._warmup_in_flight = True
        self._applyWarmupRow()

        def work() -> None:
            try:
                self._warmup_pending = warmup.run_once(self.warmup_config)
            except BaseException:  # noqa: BLE001 這裡漏接會讓暖機從此不再啟動
                warmup.log_exception("背景暖機執行緒炸掉")
            finally:
                self.performSelectorOnMainThread_withObject_waitUntilDone_(
                    "warmupFinished:",
                    None,
                    False,
                )

        threading.Thread(target=work, daemon=True).start()

    @objc.python_method
    def _applyWarmupRow(self) -> None:
        row = self.rows.get("warmup") if self.rows else None
        if row is None:
            return
        if self._warmup_in_flight:
            row.setTitleText_("每日暖機 · 暖機中…")
            row.setMetaText_("")
            row.setEnabledState_(False)
            return
        title, meta = warmup.row_texts(self.warmup_config, time.time())
        row.setTitleText_(title)
        row.setMetaText_(meta)
        row.setEnabledState_(True)

    def quitApp_(self, sender: Any) -> None:
        if self.timer is not None:
            self.timer.invalidate()
        NSApp.terminate_(sender)

    @objc.python_method
    def _refresh(self) -> None:
        if self._refresh_in_flight:
            return
        self._refresh_in_flight = True
        self.refresh_started_at = time.monotonic()
        self._setRefreshing(True)
        thread = threading.Thread(target=self._refresh_in_background, daemon=True)
        thread.start()

    @objc.python_method
    def _refresh_in_background(self) -> None:
        force_line = bool(self.force_line)
        try:
            outcome = asyncio.run(self._fetch())
            codex_rows, codex_5h_pct = self._codex_rows()
            line_rows = (
                self._line_row(line_quota.BOT_NOTICE, force=force_line),
                self._line_row(line_quota.BOT_DEV, force=force_line),
            )
            state = self._state_from_outcome(outcome, codex_rows, line_rows)
        except Exception as exc:
            codex_5h_pct = None
            state = _error_state(type(exc).__name__, self.mock)
        finally:
            # Always clear the guard from the worker thread so a failed or
            # undelivered main-thread callback can never latch the button dead.
            self._refresh_in_flight = False
            self.force_line = False

        elapsed = time.monotonic() - float(self.refresh_started_at or 0.0)
        if elapsed < 0.3:
            time.sleep(0.3 - elapsed)

        result = {"state": state, "codex_5h_pct": codex_5h_pct}
        self.performSelectorOnMainThread_withObject_waitUntilDone_modes_(
            "_applyRefreshResult:",
            result,
            False,
            [NSRunLoopCommonModes, NSEventTrackingRunLoopMode, NSDefaultRunLoopMode],
        )

    def _applyRefreshResult_(self, result: dict[str, Any]) -> None:
        state = result["state"]
        state.updated_at = time.time()
        self.codex_5h_pct = result["codex_5h_pct"]
        self.latest_state = state
        self._applyState(state)
        self.status_item.button().setTitle_(self._compose_title(state))
        self._setRefreshing(False)
        self._refresh_in_flight = False

    @objc.python_method
    def _applyState(self, state: MenuState) -> None:
        if not self.rows:
            return
        self.rows["header"].setVersionText_(state.version_text)
        self.rows["claude_session"].setRowState_(state.claude_session)
        self.rows["claude_weekly"].setRowState_(state.claude_weekly)
        self.rows["claude_fable"].setRowState_(state.claude_fable)
        self.rows["codex_session"].setRowState_(state.codex_session)
        self.rows["codex_weekly"].setRowState_(state.codex_weekly)
        self.rows["line_month"].setRowState_(state.line_month)
        self.rows["line_dev_month"].setRowState_(state.line_dev_month)
        self.rows["today"].setText_(state.today_text)
        self.rows["status"].setText_(state.status_text)
        self.rows["refresh"].setMetaText_(format_updated_ago(state.updated_at))
        self._applyWarmupRow()

    @objc.python_method
    def _setRefreshing(self, refreshing: bool) -> None:
        if not self.rows:
            return
        refresh_row = self.rows["refresh"]
        refresh_row.setTitleText_("更新中…" if refreshing else "立即更新")
        refresh_row.setEnabledState_(not refreshing)
        if refreshing:
            refresh_row.setMetaText_("")
        else:
            refresh_row.setMetaText_(format_updated_ago(self.latest_state.updated_at))

    @objc.python_method
    async def _fetch(self) -> PollOutcome:
        client = ClaudeUsageClient(mock=self.mock)
        try:
            return await client.fetch_once()
        finally:
            await client.aclose()

    @objc.python_method
    def _state_from_outcome(
        self,
        outcome: PollOutcome,
        codex_rows: tuple[QuotaRowState, QuotaRowState],
        line_rows: tuple[QuotaRowState, QuotaRowState],
    ) -> MenuState:
        now = time.time()
        group_name = GROUP_NAMES[self.tracker.group()]

        # 優先用官方用量 API：即時、不必等 Claude Code 刷 statusLine，
        # 而且只有它拿得到依模型細分的週額度（Fable 5）。
        api_usage = self._claude_api_usage()
        fable = api_usage.scoped(claude_api_loader.FABLE_KEY) if api_usage else None
        claude_fable = _quota_row(
            "每週 · Fable 5",
            fable.percent if fable else None,
            fable.resets_at if fable else None,
            now,
        )

        if api_usage is not None and (api_usage.five_hour or api_usage.seven_day):
            five = api_usage.five_hour
            weekly = api_usage.seven_day
            claude_session = _quota_row(
                "5 小時限制",
                five.percent if five else None,
                five.resets_at if five else None,
                now,
            )
            claude_weekly = _quota_row(
                "每週 · 全部模型",
                weekly.percent if weekly else None,
                weekly.resets_at if weekly else None,
                now,
            )
            status_note = "✓ 已同步（API）"
        elif outcome.state == PollState.SUCCESS and outcome.snapshot is not None:
            snapshot = outcome.snapshot
            claude_session = _quota_row(
                "5 小時限制",
                float(snapshot.current_percent),
                snapshot.current_reset_at,
                now,
            )
            claude_weekly = _quota_row(
                "每週 · 全部模型",
                float(snapshot.weekly_percent),
                snapshot.weekly_reset_at,
                now,
            )
            status_note = outcome.message or "✓ 已同步"
        else:
            claude_session = _missing_row("5 小時限制")
            claude_weekly = _missing_row("每週 · 全部模型")
            status_note = outcome.message or "無資料"

        return MenuState(
            claude_session=claude_session,
            claude_weekly=claude_weekly,
            claude_fable=claude_fable,
            codex_session=codex_rows[0],
            codex_weekly=codex_rows[1],
            line_month=line_rows[0],
            line_dev_month=line_rows[1],
            version_text=_claude_code_version(self.mock),
            today_text=_today_text(self.mock),
            status_text=f"速率 {group_name} · {status_note}",
        )

    @objc.python_method
    def _claude_api_usage(self) -> claude_api_loader.ClaudeApiUsage | None:
        """抓官方用量 API；任何失敗都回 None，交給本地 status 檔 fallback。"""
        try:
            return claude_api_loader.fetch(mock=bool(self.mock), force=bool(self.force_line))
        except Exception:
            return None

    @objc.python_method
    def _codex_rows(self) -> tuple[tuple[QuotaRowState, QuotaRowState], int | None]:
        now = time.time()
        if self.mock:
            rows = (
                _quota_row("5 小時限制", 12.0, now + (4 * 3600) + (15 * 60), now),
                _quota_row("每週 · 7 天", 28.0, now + (4 * 86400), now),
            )
            return rows, 12

        try:
            rate_limits = codex_loader.load_rate_limits_best()
        except Exception:
            rate_limits = None

        if rate_limits is None:
            return (_missing_row("5 小時限制"), _missing_row("每週 · 7 天")), None

        codex_5h_pct = (
            round(rate_limits.five_hour_pct) if rate_limits.five_hour_pct is not None else None
        )
        rows = (
            _quota_row(
                "5 小時限制",
                rate_limits.five_hour_pct,
                rate_limits.five_hour_resets_at,
                now,
            ),
            _quota_row(
                "每週 · 7 天",
                rate_limits.seven_day_pct,
                rate_limits.seven_day_resets_at,
                now,
            ),
        )
        return rows, codex_5h_pct

    @objc.python_method
    def _line_row(self, bot: str, *, force: bool = False) -> QuotaRowState:
        title = LINE_ROW_TITLES[bot]
        try:
            result = line_quota.fetch(bot, mock=bool(self.mock), force=force)
        except Exception as exc:  # 網路層以外的意外，不能拖垮整個刷新
            return QuotaRowState(title, None, f"-- · {type(exc).__name__}", available=False)

        if not result.available:
            return QuotaRowState(title, None, f"-- · {result.message}", available=False)

        if result.limit is None:
            return QuotaRowState(
                title,
                None,
                f"已發 {result.used:,} 則 · 無上限",
                available=False,
            )

        remaining = format_human_time((result.resets_at or time.time()) - time.time())
        percent = result.percent or 0.0
        head = "已用滿" if percent >= 100 else f"{_format_percent(percent)}%"
        return QuotaRowState(
            title,
            percent,
            f"{result.used:,}/{result.limit:,} · {head} · {remaining} 後重置",
            available=True,
        )

    @objc.python_method
    def _compose_title(self, state: MenuState) -> str:
        claude_pct = state.claude_session.percent
        base = "Claude --" if claude_pct is None else f"Claude {_format_percent(claude_pct)}%"
        if self.codex_5h_pct is None:
            return base
        return f"{base} · Codex {self.codex_5h_pct}%"


def run_app(mock: bool = False, interval: int = 60) -> None:
    global _APP_DELEGATE
    app = NSApplication.sharedApplication()
    _APP_DELEGATE = AppDelegate.alloc().initWithMock_interval_(mock, interval)
    app.setDelegate_(_APP_DELEGATE)
    app.run()


def _add_view_item(menu: Any, view: Any) -> Any:
    item = NSMenuItem.alloc().init()
    item.setView_(view)
    menu.addItem_(item)
    return view


def _add_quota_row(menu: Any) -> QuotaRowView:
    view = QuotaRowView.alloc().initWithFrame_(NSMakeRect(0, 0, MENU_WIDTH, QUOTA_ROW_HEIGHT))
    return _add_view_item(menu, view)


def _add_info_row(menu: Any) -> InfoRowView:
    view = InfoRowView.alloc().initWithFrame_(NSMakeRect(0, 0, MENU_WIDTH, INFO_ROW_HEIGHT))
    return _add_view_item(menu, view)


def _image_view(path: str) -> Any:
    image = NSImage.alloc().initWithContentsOfFile_(path)
    if image is None:
        return None
    image.setSize_(NSMakeSize(17, 17))
    view = NSImageView.alloc().initWithFrame_(NSMakeRect(0, 0, 17, 17))
    view.setImage_(image)
    view.setImageScaling_(NSImageScaleProportionallyUpOrDown)
    return view


def _empty_state() -> MenuState:
    return MenuState(
        claude_session=_missing_row("5 小時限制"),
        claude_weekly=_missing_row("每週 · 全部模型"),
        claude_fable=_missing_row("每週 · Fable 5"),
        codex_session=_missing_row("5 小時限制"),
        codex_weekly=_missing_row("每週 · 7 天"),
        line_month=_missing_row(LINE_ROW_TITLES[line_quota.BOT_NOTICE]),
        line_dev_month=_missing_row(LINE_ROW_TITLES[line_quota.BOT_DEV]),
        version_text="",
        today_text="今日 $0.00 · 0 tokens",
        status_text="載入中…",
    )


def _error_state(message: str, mock: bool) -> MenuState:
    state = _empty_state()
    state.status_text = f"錯誤（{message}）"
    state.today_text = _today_text(mock)
    return state


def _quota_row(
    title: str,
    pct: float | None,
    resets_at: float | None,
    now: float,
) -> QuotaRowState:
    if pct is None or resets_at is None:
        return _missing_row(title)
    pct = max(0.0, min(100.0, float(pct)))
    return QuotaRowState(
        title=title,
        percent=pct,
        meta_text=f"{_format_percent(pct)}% · {format_human_time(resets_at - now)} 後重置",
        available=True,
    )


def _missing_row(title: str) -> QuotaRowState:
    return QuotaRowState(title=title, percent=None, meta_text="--", available=False)


def _claude_code_version(mock: bool = False) -> str:
    if mock:
        return "v2.1.220"
    try:
        with open(STATUS_FILE, encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError):
        return ""
    version = data.get("version")
    return f"v{version}" if isinstance(version, str) else ""


def _today_text(mock: bool = False) -> str:
    if mock:
        return "今日 $45.20 · 50,193,442 tokens"

    today = datetime.now().astimezone().date()
    total_tokens = 0
    total_cost = 0.0

    entries = load_entries(hours_back=24) + codex_loader.load_entries(hours_back=24)
    for entry in entries:
        if entry.timestamp.astimezone().date() != today:
            continue
        total_tokens += entry.total_tokens
        total_cost += calculate_cost(entry)

    return f"今日 ${total_cost:.2f} · {total_tokens:,} tokens"


def _format_percent(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.1f}"


def _label(
    text: str,
    font: NSFont,
    color: NSColor,
    alignment: int | None = None,
) -> NSTextField:
    label = NSTextField.labelWithString_(text)
    label.setFont_(font)
    label.setTextColor_(color)
    label.setDrawsBackground_(False)
    label.setBordered_(False)
    label.setEditable_(False)
    label.setSelectable_(False)
    if alignment is not None:
        label.setAlignment_(alignment)
    return label


def _label_color() -> NSColor:
    return NSColor.labelColor()


def _secondary_color() -> NSColor:
    return NSColor.secondaryLabelColor()


def _tertiary_color() -> NSColor:
    return NSColor.tertiaryLabelColor()


def _track_color() -> NSColor:
    return NSColor.labelColor().colorWithAlphaComponent_(0.12)


def _bar_color(percent: float) -> NSColor:
    if percent >= DANGER_PERCENT:
        return NSColor.systemRedColor()
    if percent >= WARN_PERCENT:
        return NSColor.systemOrangeColor()
    return NSColor.labelColor().colorWithAlphaComponent_(0.55)


def _meta_color(row: QuotaRowState) -> NSColor:
    if not row.available or row.percent is None:
        return NSColor.tertiaryLabelColor()
    if row.percent >= DANGER_PERCENT:
        return NSColor.systemRedColor()
    if row.percent >= WARN_PERCENT:
        return NSColor.systemOrangeColor()
    return NSColor.secondaryLabelColor()


def _fill_rounded_rect(rect: Any, radius: float) -> None:
    NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(rect, radius, radius).fill()
