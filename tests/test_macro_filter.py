from datetime import datetime, timezone

from modules import macro_filter


DECISION_TIME = datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)


def _event(time, impact="high", currency="USD", title="CPI"):
    return {
        "date": "2026-06-11",
        "time": time,
        "currency": currency,
        "impact": impact,
        "event": title,
    }


def test_high_impact_blocks_inside_before_window():
    result = macro_filter.get_macro_risk(
        "EUR/USD",
        DECISION_TIME,
        events=[_event("12:30")],
    )
    assert result["macro_block"] is True
    assert result["macro_risk_level"] == "high"
    assert result["macro_minutes_distance"] == 30.0
    assert result["macro_reason"] == "high_impact_macro_event"


def test_high_impact_blocks_inside_after_window():
    result = macro_filter.get_macro_risk(
        "EUR/USD",
        DECISION_TIME,
        events=[_event("11:30")],
    )
    assert result["macro_block"] is True
    assert result["macro_minutes_distance"] == -30.0


def test_medium_impact_reduces_confidence_without_blocking():
    result = macro_filter.get_macro_risk(
        "EUR/USD",
        DECISION_TIME,
        events=[_event("12:20", impact="medium")],
    )
    assert result["macro_block"] is False
    assert result["macro_risk_level"] == "medium"
    assert result["macro_context_snapshot"]["has_confidence_reduction"] is True


def test_low_or_unrelated_events_are_ignored():
    result = macro_filter.get_macro_risk(
        "EUR/USD",
        DECISION_TIME,
        events=[
            _event("12:00", impact="low"),
            _event("12:00", currency="JPY"),
        ],
    )
    assert result["macro_block"] is False
    assert result["macro_risk_level"] == "none"


def test_disabled_filter_is_non_blocking(monkeypatch):
    monkeypatch.setenv("USE_ECONOMIC_CALENDAR_FILTER", "false")
    result = macro_filter.get_macro_risk(
        "EUR/USD",
        DECISION_TIME,
        events=[_event("12:00")],
    )
    assert result["macro_block"] is False
    assert result["macro_risk_level"] == "none"
