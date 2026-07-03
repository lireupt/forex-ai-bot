"""Testes para `modules.decision_engine` — motor de decisão puro (Passo 3)."""

from datetime import datetime, timezone

import pandas as pd
import pytest

from modules import decision_engine as de
from modules.pair_spec import get_pair_spec

EURUSD = get_pair_spec("EUR/USD")
NOW = datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc)


def _event(title, country, event_time, source="test"):
    return {"title": title, "country": country, "impact": "high", "event_time": event_time, "source": source}


class TestResolveEventGate:
    def test_disabled_returns_no_event(self):
        result = de.resolve_event_gate([_event("CPI", "USD", NOW.isoformat())], NOW, 120, enabled=False)
        assert result["dangerous_event_nearby"] is False
        assert result["event_gate_reason"] == "event_filter_disabled"

    def test_nearby_whitelisted_event_blocks(self):
        near = (NOW.replace(minute=30)).isoformat()
        result = de.resolve_event_gate([_event("Core CPI", "USD", near)], NOW, 120)
        assert result["dangerous_event_nearby"] is True
        assert result["event"]["currency"] == "USD"

    def test_non_whitelisted_event_ignored(self):
        near = (NOW.replace(minute=30)).isoformat()
        result = de.resolve_event_gate([_event("Some Minor Speech", "USD", near)], NOW, 120)
        assert result["dangerous_event_nearby"] is False
        assert result["ignored_events"][0]["reason"] == "event_ignored_not_whitelisted"

    def test_wrong_currency_ignored(self):
        near = (NOW.replace(minute=30)).isoformat()
        result = de.resolve_event_gate(
            [_event("CPI", "JPY", near)], NOW, 120, relevant_currencies={"USD", "EUR"}
        )
        assert result["dangerous_event_nearby"] is False
        assert result["ignored_events"][0]["reason"] == "event_ignored_wrong_currency"

    def test_event_outside_window_ignored(self):
        far = (NOW.replace(hour=20)).isoformat()
        result = de.resolve_event_gate([_event("CPI", "USD", far)], NOW, 60)
        assert result["dangerous_event_nearby"] is False
        assert result["ignored_events"] == []


class TestCooldownState:
    def _config(self, **overrides):
        base = {
            "enabled": True,
            "cooldown_minutes": 120,
            "after_loss_hours": 3,
            "max_direction_signals_per_day": 1,
        }
        base.update(overrides)
        return base

    def test_disabled_returns_inactive(self):
        state = de.cooldown_state([], None, "BUY", NOW, self._config(enabled=False))
        assert state["cooldown_active"] is False

    def test_recent_same_direction_trade_triggers_cooldown(self):
        trades = [{"direction": "BUY", "created_at": (NOW.replace(hour=11)).isoformat()}]
        state = de.cooldown_state(trades, None, "BUY", NOW, self._config())
        assert state["cooldown_active"] is True
        assert state["reason"] == "cooldown_active"

    def test_opposite_direction_does_not_trigger_cooldown_window(self):
        trades = [{"direction": "SELL", "created_at": (NOW.replace(hour=11)).isoformat()}]
        state = de.cooldown_state(trades, None, "BUY", NOW, self._config())
        assert state["cooldown_active"] is False

    def test_max_direction_signals_per_day_reached(self):
        earlier_today = NOW.replace(hour=1).isoformat()
        trades = [{"direction": "BUY", "created_at": earlier_today}]
        state = de.cooldown_state(trades, None, "BUY", NOW, self._config(cooldown_minutes=30))
        assert state["max_direction_signals_reached"] is True

    def test_recent_loss_triggers_cooldown(self):
        last_closed = {"status": "loss", "closed_at": (NOW.replace(hour=11)).isoformat()}
        state = de.cooldown_state([], last_closed, "BUY", NOW, self._config(cooldown_minutes=1))
        assert state["cooldown_active"] is True
        assert "hours_since_loss" in state

    def test_old_loss_does_not_trigger_cooldown(self):
        last_closed = {"status": "loss", "closed_at": "2026-05-01T00:00:00+00:00"}
        state = de.cooldown_state([], last_closed, "BUY", NOW, self._config(cooldown_minutes=1))
        assert state["cooldown_active"] is False


class TestSignalPersistence:
    def test_neutral_direction_is_zero(self):
        assert de.signal_persistence_from_decisions([{"gating_signal": "BUY"}], "NEUTRAL") == 0

    def test_counts_consecutive_matches(self):
        decisions = [
            {"gating_signal": "BUY"},
            {"gating_signal": "BUY"},
            {"gating_signal": "SELL"},
        ]
        assert de.signal_persistence_from_decisions(decisions, "BUY") == 3

    def test_breaks_on_first_mismatch(self):
        decisions = [{"gating_signal": "SELL"}, {"gating_signal": "BUY"}]
        assert de.signal_persistence_from_decisions(decisions, "BUY") == 1


class TestRiskPerformance:
    def test_loss_streak_from_most_recent_trades(self):
        trades = [
            {"pair": "EUR/USD", "status": "loss", "result_r_multiple": -1.0},
            {"pair": "EUR/USD", "status": "loss", "result_r_multiple": -1.0},
            {"pair": "EUR/USD", "status": "win", "result_r_multiple": 2.0},
        ]
        result = de.risk_performance(trades, [], "EUR/USD")
        assert result["loss_streak"] == 2

    def test_win_breaks_loss_streak_immediately(self):
        trades = [{"pair": "EUR/USD", "status": "win", "result_r_multiple": 2.0}]
        result = de.risk_performance(trades, [], "EUR/USD")
        assert result["loss_streak"] == 0

    def test_winrate_computed_from_closed_trades(self):
        trades = [
            {"pair": "EUR/USD", "status": "win", "result_r_multiple": 2.0},
            {"pair": "EUR/USD", "status": "loss", "result_r_multiple": -1.0},
        ]
        result = de.risk_performance(trades, [], "EUR/USD")
        assert result["winrate"] == 50.0


class TestComputeTradeLevels:
    def test_buy_uses_pair_spec_defaults(self):
        created_at = datetime(2026, 5, 6, 19, 0, tzinfo=timezone.utc)
        levels = de.compute_trade_levels("BUY", 1.1700, 20.0, EURUSD, created_at, "1h")
        assert levels["sl_pips"] == 20.0
        assert levels["tp_pips"] == 40.0
        assert levels["expiry_at"] == "2026-05-07T01:00:00+00:00"

    def test_neutral_direction_returns_none(self):
        assert de.compute_trade_levels("NEUTRAL", 1.17, 20.0, EURUSD, NOW, "1h") is None

    def test_explicit_mults_override_pair_spec(self):
        created_at = datetime(2026, 5, 6, 19, 0, tzinfo=timezone.utc)
        levels = de.compute_trade_levels(
            "BUY", 1.17, 10.0, EURUSD, created_at, "1h", sl_mult=2.0, tp_mult=3.0, expiry_bars=12
        )
        assert levels["sl_pips"] == 20.0
        assert levels["tp_pips"] == 30.0


class TestDecideSmoke:
    def test_decide_with_empty_candles_is_neutral_and_does_not_crash(self):
        empty = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        ctx = de.MarketContext(
            pair="EUR/USD",
            timeframe="1h",
            now=NOW,
            pair_spec=EURUSD,
            candles_by_timeframe={"m15": empty, "h1": empty, "h4": empty, "d1": empty},
        )
        decision = de.decide(ctx)
        assert decision.signal == "NEUTRAL"
        assert decision.trade_allowed is False
        assert decision.trade_params is None

    def test_decide_defaults_ai_result_when_none(self):
        empty = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        ctx = de.MarketContext(
            pair="EUR/USD",
            timeframe="1h",
            now=NOW,
            pair_spec=EURUSD,
            candles_by_timeframe={"m15": empty, "h1": empty, "h4": empty, "d1": empty},
            ai_result=None,
        )
        decision = de.decide(ctx)
        assert decision.ai_result["signal"] == "NEUTRAL"
        assert decision.ai_score == 0.0
