from __future__ import annotations

import time

import pytest

import claude_api_loader

# 取自 GET /api/oauth/usage 的真實回應形狀（數值改過）
SAMPLE_PAYLOAD = {
    "five_hour": {"utilization": 12.0, "resets_at": "2026-08-18T10:10:00.349425+00:00"},
    "seven_day": {"utilization": 19.0, "resets_at": "2026-08-23T01:00:00.349445+00:00"},
    "seven_day_opus": None,
    "limits": [
        {
            "kind": "session",
            "percent": 12,
            "resets_at": "2026-08-18T10:10:00.349425+00:00",
            "scope": None,
        },
        {
            "kind": "weekly_all",
            "percent": 19,
            "resets_at": "2026-08-23T01:00:00.349445+00:00",
            "scope": None,
        },
        {
            "kind": "weekly_scoped",
            "percent": 42,
            "resets_at": "2026-08-23T01:00:00.349590+00:00",
            "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None},
        },
    ],
}


def test_parse_extracts_totals_and_fable_weekly() -> None:
    usage = claude_api_loader._parse(SAMPLE_PAYLOAD)

    assert usage.five_hour is not None
    assert usage.five_hour.percent == 12.0
    assert usage.seven_day is not None
    assert usage.seven_day.percent == 19.0

    fable = usage.scoped(claude_api_loader.FABLE_KEY)
    assert fable is not None
    assert fable.percent == 42.0
    assert fable.resets_at is not None


def test_scoped_matches_display_name_with_version_suffix() -> None:
    payload = {
        "limits": [
            {
                "kind": "weekly_scoped",
                "percent": 7,
                "resets_at": None,
                "scope": {"model": {"display_name": "Fable 5"}},
            }
        ]
    }

    usage = claude_api_loader._parse(payload)

    assert usage.scoped("fable") is not None
    assert usage.scoped("fable").percent == 7.0
    assert usage.scoped("opus") is None


def test_parse_survives_missing_and_malformed_limits() -> None:
    usage = claude_api_loader._parse({"five_hour": None, "limits": ["nope", {}, None]})

    assert usage.five_hour is None
    assert usage.seven_day is None
    assert usage.weekly_scoped == {}


def test_access_token_rejects_expired_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        claude_api_loader,
        "_keychain_credentials",
        lambda: {"accessToken": "sk-ant-oat01-x", "expiresAt": (time.time() - 10) * 1000},
    )

    assert claude_api_loader._access_token() is None


def test_access_token_accepts_live_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        claude_api_loader,
        "_keychain_credentials",
        lambda: {"accessToken": "sk-ant-oat01-x", "expiresAt": (time.time() + 3600) * 1000},
    )

    assert claude_api_loader._access_token() == "sk-ant-oat01-x"


def test_fetch_returns_none_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claude_api_loader, "_cache", None)
    monkeypatch.setattr(claude_api_loader, "_access_token", lambda: None)

    assert claude_api_loader.fetch(force=True) is None


def test_mock_usage_includes_fable_row() -> None:
    usage = claude_api_loader.fetch(mock=True)

    assert usage is not None
    assert usage.scoped(claude_api_loader.FABLE_KEY) is not None
