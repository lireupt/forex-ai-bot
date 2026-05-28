from datetime import datetime

from modules.operational import operational_state


def test_trade_window_allowed():
    state = operational_state(datetime(2026, 5, 18, 8, 5), mode="trade")
    assert state["can_open_trade"] is True
    assert state["block_reason"] == ""


def test_outside_trade_window_blocked():
    state = operational_state(datetime(2026, 5, 18, 16, 0), mode="trade")
    assert state["can_open_trade"] is False
    assert state["block_reason"] == "outside_operational_trade_window"


def test_env_trade_window(monkeypatch):
    monkeypatch.setenv("OPERATIONAL_TRADE_START_HOUR", "9")
    monkeypatch.setenv("OPERATIONAL_TRADE_END_HOUR", "11")
    assert operational_state(datetime(2026, 5, 18, 8, 59), mode="trade")["can_open_trade"] is False
    assert operational_state(datetime(2026, 5, 18, 9, 0), mode="trade")["can_open_trade"] is True
    assert operational_state(datetime(2026, 5, 18, 10, 59), mode="trade")["can_open_trade"] is True
    assert operational_state(datetime(2026, 5, 18, 11, 0), mode="trade")["can_open_trade"] is False


def test_analysis_mode_never_opens_trade():
    state = operational_state(datetime(2026, 5, 18, 8, 5), mode="analysis")
    assert state["can_open_trade"] is False
    assert state["block_reason"] == "analysis_mode"


def test_night_collection_without_trade_opening():
    state = operational_state(datetime(2026, 5, 18, 2, 0), mode="trade")
    assert state["can_open_trade"] is False
    assert state["is_night_collection"] is True
    assert state["block_reason"] == "night_collection_only"


def test_weekend_blocked():
    state = operational_state(datetime(2026, 5, 16, 8, 5), mode="trade")
    assert state["can_open_trade"] is False
    assert state["block_reason"] == "weekend"
