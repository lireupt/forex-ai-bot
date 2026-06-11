"""Testes para `modules.context_snapshot` — montagem do snapshot agregador."""

from modules import context_snapshot


TECHNICAL_RESULT = {
    "signal": "BUY",
    "technical_score_m15": 0.2,
    "technical_score_h1": 0.5,
    "technical_score_h4": 0.4,
    "technical_score_d1": 0.1,
    "multi_timeframe_score": 0.38,
    "timeframe_alignment": "h1_h4_aligned",
    "indicators": {
        "current_price": 1.1756,
        "rsi": 58.2,
        "rsi_signal": "neutral",
        "ema20": 1.1747,
        "ema50": 1.1732,
        "ema_trend": "bullish",
        "macd": 0.0011,
        "macd_signal": "bearish",
        "macd_signal_value": 0.0013,
        "atr_pips": 12.6,
        "volatility_reason": "Volatilidade normal",
        "adx": 22.0,
        "technical_score": 0.42,
    },
}

AI_RESULT = {
    "signal": "NEUTRAL",
    "bias": "NEUTRAL",
    "confidence": 20,
    "macro_context": "mixed",
    "news_sentiment": "neutral",
    "volatility_context": "medium",
    "reason": "Sem catalisador claro.",
}

COMBINED = {"signal": "BUY", "confidence": 41, "combined_score": 0.41}
GATING = {"signal": "BUY", "confidence": 41, "hold_off": False}
TRADE_DECISION = {
    "trade_allowed": True,
    "block_reason": None,
    "gate_reasons": [],
    "gate_diagnostics": {
        "config": {
            "dry_run": True,
            "allow_buy": True,
            "allow_sell": True,
            "block_near_high_impact_events": True,
        }
    },
}
GATE_CONTEXT = {
    "market": {"is_open": True, "session": "london"},
    "operational": {"can_open_trade": True, "block_reason": ""},
    "cooldown": {"cooldown_active": False},
    "signal_persistence": 2,
    "spread_pips": 0.8,
    "macro": {
        "macro_risk_level": "medium",
        "macro_block": False,
        "macro_event_title": "CPI",
        "macro_event_currency": "USD",
        "macro_event_time": "2026-05-06T19:10:00+00:00",
        "macro_minutes_distance": 10.0,
        "macro_reason": "medium_impact_macro_event",
        "macro_context_snapshot": {"has_confidence_reduction": True},
    },
}
EVENT_RISK = {"dangerous_event_nearby": False, "dangerous_event_reason": ""}
PERFORMANCE = {"winrate": 50.0, "loss_streak": 1, "net_pips": 12.0}


class TestBuildMarketSnapshot:
    def _snapshot(self):
        return context_snapshot.build_market_snapshot(
            "EUR/USD",
            TECHNICAL_RESULT,
            AI_RESULT,
            COMBINED,
            GATING,
            TRADE_DECISION,
            GATE_CONTEXT,
            EVENT_RISK,
            PERFORMANCE,
            gating_mode="score",
        )

    def test_has_all_layers(self):
        snap = self._snapshot()
        for key in (
            "technical", "fundamental", "performance",
            "operational_risk", "filters", "preliminary_recommendation",
        ):
            assert key in snap

    def test_technical_layer_values(self):
        snap = self._snapshot()
        assert snap["technical"]["rsi"] == 58.2
        assert snap["technical"]["multi_timeframe_score"] == 0.38
        assert snap["technical"]["timeframe_alignment"] == "h1_h4_aligned"

    def test_fundamental_layer_values(self):
        snap = self._snapshot()
        assert snap["fundamental"]["ai_bias"] == "NEUTRAL"
        assert snap["fundamental"]["dangerous_event_nearby"] is False

    def test_filters_pulled_from_gate_diagnostics(self):
        snap = self._snapshot()
        assert snap["filters"]["dry_run"] is True
        assert snap["filters"]["allow_sell"] is True
        assert snap["filters"]["block_near_high_impact_events"] is True
        assert snap["filters"]["trade_allowed"] is True

    def test_preliminary_recommendation(self):
        snap = self._snapshot()
        assert snap["preliminary_recommendation"]["combined_signal"] == "BUY"
        assert snap["preliminary_recommendation"]["gating_mode"] == "score"
        assert snap["preliminary_recommendation"]["hold_off"] is False

    def test_includes_macro_calendar_context(self):
        snap = self._snapshot()
        assert snap["macro_calendar"]["risk_level"] == "medium"
        assert snap["macro_calendar"]["event_title"] == "CPI"
        assert snap["macro_calendar"]["context_snapshot"]["has_confidence_reduction"] is True

    def test_tolerates_missing_pieces(self):
        snap = context_snapshot.build_market_snapshot(
            "EUR/USD", {}, {}, {}, {}, {}, {}, {}, {},
        )
        assert snap["pair"] == "EUR/USD"
        assert snap["technical"]["rsi"] is None


class TestBuildPerformanceSnapshot:
    def test_uses_recent_performance_and_summary(self, memory_db):
        snap = context_snapshot.build_performance_snapshot(
            memory_db, "EUR/USD", recent_performance={"loss_streak": 3, "max_drawdown": 2.1}
        )
        assert snap["loss_streak"] == 3
        assert snap["max_drawdown"] == 2.1
        assert snap["window_days"] == 7
        # Sem trades na DB vazia, mas as chaves de calibração devem existir.
        assert "blocked_by_reason" in snap
        assert "buy_vs_sell" in snap

    def test_resilient_when_summary_fails(self, monkeypatch, memory_db):
        monkeypatch.setattr(
            context_snapshot.database,
            "get_calibration_summary",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        snap = context_snapshot.build_performance_snapshot(
            memory_db, "EUR/USD", recent_performance={"loss_streak": 0}
        )
        assert snap["loss_streak"] == 0
        assert snap["total_decisions"] is None
