from datetime import datetime

from modules.operational import operational_state


def test_trade_window_allowed():
    state = operational_state(datetime(2026, 5, 18, 8, 5), mode="trade")
    assert state["can_open_trade"] is True
    assert state["block_reason"] == ""


def test_outside_trade_window_blocked():
    state = operational_state(datetime(2026, 5, 18, 9, 0), mode="trade")
    assert state["can_open_trade"] is False
    assert state["block_reason"] == "outside_operational_trade_window"


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
