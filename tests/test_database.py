"""Testes para `modules.database` — schema, save_decision, paper trades."""

from datetime import datetime, timedelta, timezone

import pytest

from modules import database


def _make_decision_entry(**overrides):
    base = {
        "timestamp": "2026-05-06T19:00:00+00:00",
        "pair": "EUR/USD",
        "timeframe": "1h",
        "news_source_status": "fresh",
        "calendar_source_status": "fresh",
        "ai_source_status": "fresh",
        "candles_source_status": "fresh",
        "rsi_vote": "neutral",
        "ema_vote": "bullish",
        "macd_vote": "bearish",
        "rsi_value": 58.2,
        "ema20_value": 1.1747,
        "ema50_value": 1.1732,
        "macd_value": 0.0011,
        "macd_signal_value": 0.0013,
        "atr14_value": 0.0013,
        "atr_price": 0.0013,
        "atr_pips": 12.6,
        "volatility_reason": "Volatilidade normal",
        "technical_reason": "RSI neutral; EMA bullish; MACD bearish.",
        "shadow_technical_signal": "NEUTRAL",
        "shadow_technical_confidence": 33,
        "shadow_technical_reason": "1 bullish, 1 bearish",
        "shadow_combined_signal": "BUY",
        "shadow_combined_confidence": 36,
        "shadow_combined_reason": "apenas IA (shadow NEUTRAL)",
        "technical_signal": "NEUTRAL",
        "ai_signal": "BUY",
        "combined_signal": "NEUTRAL",
        "confidence": 0,
        "hold_off": True,
        "current_price": 1.1756,
        "trade_allowed": False,
        "block_reason": "sinal combinado é NEUTRAL",
        "dangerous_event_nearby": False,
        "dangerous_event_reason": "",
        "simulated_order": None,
        "decision_signature": "abc123",
        "stop_loss_pips_used": None,
        "take_profit_pips_used": None,
        "sl_tp_mode": None,
        "ai_score": 0.6,
        "ai_confidence_score": 0.6,
        "ai_analysis_text": "Full AI market assessment with fundamentals and technical context.",
        "ai_reason": "AI thinks BUY",
        "ai_features_snapshot": {"close": 1.1756, "rsi": 58.2},
        "ai_model_version": "groq:llama-3.3-70b-versatile",
        "ai_bias": "BUY",
        "ai_confidence_adjustment": 0.12,
        "ai_risk_adjustment": -0.05,
        "macro_context": "bullish_eur",
        "volatility_context": "medium",
        "news_sentiment": "positive",
        "ai_context_reason": "Fed dovish.",
        "technical_score": 0.0,
        "shadow_score": 0.0,
        "combined_score": 0.36,
        "combined_reason": "AI=BUY (+0.60), tech=NEUTRAL (+0.00) -> +0.36",
        "blocking_reason": "sinal combinado é NEUTRAL",
        "score_combined_signal": "BUY",
        "operational_mode": "trade",
        "operational_can_trade": True,
        "operational_block_reason": "",
    }
    base.update(overrides)
    return base


def _make_paper_trade(decision_id, **overrides):
    base = {
        "decision_id": decision_id,
        "pair": "EUR/USD",
        "timeframe": "1h",
        "direction": "BUY",
        "entry_price": 1.1756,
        "simulated_sl": 1.17434,
        "simulated_tp": 1.17812,
        "sl_pips": 12.6,
        "tp_pips": 25.2,
        "atr_pips": 12.6,
        "atr_price": 0.00126,
        "status": "open",
        "source": "ai_only",
        "signal_source": "ai_signal",
        "created_at": "2026-05-06T19:00:00+00:00",
        "expiry_at": "2026-05-07T01:00:00+00:00",
    }
    base.update(overrides)
    return base


class TestSchema:
    def test_init_creates_decisions_columns(self, memory_db):
        cols = {row["name"] for row in memory_db.execute("PRAGMA table_info(decisions)").fetchall()}
        # As novas colunas têm de estar presentes
        for col in [
            "ai_score", "ai_confidence_score", "ai_analysis_text", "ai_reason",
            "ai_features_snapshot", "ai_model_version", "technical_score",
            "ai_bias", "ai_confidence_adjustment", "ai_risk_adjustment",
            "macro_context", "volatility_context", "news_sentiment",
            "ai_context_reason",
            "technical_score_m15", "technical_score_h1", "technical_score_h4",
            "technical_score_d1", "multi_timeframe_score", "timeframe_alignment",
            "timeframe_block_reason",
            "shadow_score", "combined_score",
            "combined_reason", "blocking_reason", "score_combined_signal", "paper_trade_id",
            "operational_mode", "operational_can_trade", "operational_block_reason",
            "decision_hash", "is_duplicate",
            "macro_risk_level", "macro_block", "macro_event_title",
            "macro_event_currency", "macro_event_time", "macro_minutes_distance",
            "macro_reason", "macro_context_snapshot_json",
        ]:
            assert col in cols, f"coluna {col} em falta"

    def test_init_creates_paper_trades_table(self, memory_db):
        cols = {row["name"] for row in memory_db.execute("PRAGMA table_info(paper_trades)").fetchall()}
        for col in [
            "id", "decision_id", "pair", "timeframe", "direction", "entry_price",
            "simulated_sl", "simulated_tp", "sl_pips", "tp_pips", "atr_pips",
            "status", "source", "signal_source", "created_at", "expiry_at",
            "close_price", "closed_at", "close_reason", "result_pips", "result_r_multiple",
        ]:
            assert col in cols, f"coluna {col} em falta na paper_trades"

    def test_init_is_idempotent(self, memory_db):
        # Chamar init_db duas vezes não deve falhar
        database.init_db(memory_db)
        database.init_db(memory_db)

    def test_init_creates_gate_checks_table(self, memory_db):
        cols = {row["name"] for row in memory_db.execute("PRAGMA table_info(gate_checks)").fetchall()}
        for col in [
            "id", "checked_at", "status", "total_trades", "wins", "losses",
            "expired", "win_rate", "profit_factor", "avg_r",
            "max_streak_losses", "max_drawdown_pct", "details_json",
            "config_json",
        ]:
            assert col in cols, f"coluna {col} em falta na gate_checks"

    def test_init_creates_analytics_metrics_table(self, memory_db):
        cols = {row["name"] for row in memory_db.execute("PRAGMA table_info(analytics_metrics)").fetchall()}
        for col in [
            "winrate", "average_rr", "profit_factor", "expectancy",
            "max_drawdown", "sharpe_ratio", "average_score", "ai_impact",
            "h4_d1_impact", "alignment_success_rate", "metrics_json",
        ]:
            assert col in cols, f"coluna {col} em falta na analytics_metrics"


class TestSaveDecision:
    def test_returns_lastrowid(self, memory_db):
        entry = _make_decision_entry()
        decision_id = database.save_decision(memory_db, entry)
        assert isinstance(decision_id, int)
        assert decision_id > 0

    def test_persists_all_new_fields(self, memory_db):
        entry = _make_decision_entry()
        decision_id = database.save_decision(memory_db, entry)

        row = memory_db.execute(
            "SELECT * FROM decisions WHERE id = ?", (decision_id,)
        ).fetchone()
        assert row["ai_score"] == 0.6
        assert row["ai_analysis_text"] == "Full AI market assessment with fundamentals and technical context."
        assert row["ai_model_version"] == "groq:llama-3.3-70b-versatile"
        assert row["combined_score"] == 0.36
        assert row["ai_bias"] == "BUY"
        assert row["ai_confidence_adjustment"] == 0.12
        assert row["ai_risk_adjustment"] == -0.05
        assert row["operational_mode"] == "trade"
        assert row["operational_can_trade"] == 1
        assert row["score_combined_signal"] == "BUY"
        assert row["blocking_reason"] == "sinal combinado é NEUTRAL"
        assert row["decision_hash"] == "abc123"
        assert row["is_duplicate"] == 0
        # snapshot é guardado como JSON string
        assert '"close":' in row["ai_features_snapshot"]

    def test_persists_macro_filter_fields(self, memory_db):
        entry = _make_decision_entry(
            macro_risk_level="high",
            macro_block=True,
            macro_event_title="FOMC Statement",
            macro_event_currency="USD",
            macro_event_time="2026-06-11T18:00:00+00:00",
            macro_minutes_distance=-12.0,
            macro_reason="high_impact_macro_event",
            macro_context_snapshot={
                "has_macro_block": True,
                "has_confidence_reduction": False,
            },
        )
        decision_id = database.save_decision(memory_db, entry)
        row = memory_db.execute(
            "SELECT * FROM decisions WHERE id = ?", (decision_id,)
        ).fetchone()
        assert row["macro_risk_level"] == "high"
        assert row["macro_block"] == 1
        assert row["macro_event_title"] == "FOMC Statement"
        assert row["macro_minutes_distance"] == -12.0
        assert '"has_macro_block": true' in row["macro_context_snapshot_json"]

    def test_persists_duplicate_flag_without_skipping_insert(self, memory_db):
        first_id = database.save_decision(
            memory_db,
            _make_decision_entry(decision_signature="same", decision_hash="same"),
        )
        second_id = database.save_decision(
            memory_db,
            _make_decision_entry(
                timestamp="2026-05-06T19:01:00+00:00",
                decision_signature="same",
                decision_hash="same",
                is_duplicate=True,
            ),
        )

        rows = memory_db.execute(
            """
            SELECT id, timestamp, decision_hash, is_duplicate
            FROM decisions
            ORDER BY id ASC
            """
        ).fetchall()
        assert [row["id"] for row in rows] == [first_id, second_id]
        assert rows[0]["decision_hash"] == "same"
        assert rows[0]["is_duplicate"] == 0
        assert rows[1]["decision_hash"] == "same"
        assert rows[1]["is_duplicate"] == 1
        assert rows[0]["timestamp"] != rows[1]["timestamp"]

    def test_features_snapshot_passthrough_when_string(self, memory_db):
        entry = _make_decision_entry(ai_features_snapshot='{"already":"json"}')
        decision_id = database.save_decision(memory_db, entry)
        row = memory_db.execute(
            "SELECT ai_features_snapshot FROM decisions WHERE id = ?", (decision_id,)
        ).fetchone()
        assert row["ai_features_snapshot"] == '{"already":"json"}'

    def test_get_last_signature_returns_latest(self, memory_db):
        database.save_decision(memory_db, _make_decision_entry(decision_signature="sig1"))
        database.save_decision(memory_db, _make_decision_entry(decision_signature="sig2"))
        sig, ts = database.get_last_decision_signature(memory_db, "EUR/USD")
        assert sig == "sig2"


class TestPaperTrades:
    def test_create_returns_id(self, memory_db):
        decision_id = database.save_decision(memory_db, _make_decision_entry())
        paper_trade_id = database.create_paper_trade(memory_db, _make_paper_trade(decision_id))
        assert isinstance(paper_trade_id, int)
        assert paper_trade_id > 0

    def test_link_decision_updates_fk(self, memory_db):
        decision_id = database.save_decision(memory_db, _make_decision_entry())
        paper_trade_id = database.create_paper_trade(memory_db, _make_paper_trade(decision_id))
        database.link_decision_to_paper_trade(memory_db, decision_id, paper_trade_id)

        row = memory_db.execute(
            "SELECT paper_trade_id FROM decisions WHERE id = ?", (decision_id,)
        ).fetchone()
        assert row["paper_trade_id"] == paper_trade_id

    def test_link_handles_none_safely(self, memory_db):
        # não deve crashar
        database.link_decision_to_paper_trade(memory_db, None, None)
        database.link_decision_to_paper_trade(memory_db, 1, None)
        database.link_decision_to_paper_trade(memory_db, None, 1)

    def test_get_open_returns_only_open(self, memory_db):
        decision_id = database.save_decision(memory_db, _make_decision_entry())
        open_id = database.create_paper_trade(memory_db, _make_paper_trade(decision_id))
        closed_id = database.create_paper_trade(memory_db, _make_paper_trade(decision_id, status="win"))

        opens = database.get_open_paper_trades(memory_db)
        assert len(opens) == 1
        assert opens[0]["id"] == open_id

    def test_get_open_filters_by_pair(self, memory_db):
        decision_id = database.save_decision(memory_db, _make_decision_entry())
        database.create_paper_trade(memory_db, _make_paper_trade(decision_id, pair="EUR/USD"))
        database.create_paper_trade(memory_db, _make_paper_trade(decision_id, pair="GBP/USD"))

        opens_eur = database.get_open_paper_trades(memory_db, pair="EUR/USD")
        assert len(opens_eur) == 1
        assert opens_eur[0]["pair"] == "EUR/USD"

    def test_update_paper_trade_result(self, memory_db):
        decision_id = database.save_decision(memory_db, _make_decision_entry())
        paper_trade_id = database.create_paper_trade(memory_db, _make_paper_trade(decision_id))

        database.update_paper_trade_result(
            memory_db,
            paper_trade_id=paper_trade_id,
            status="win",
            close_price=1.17812,
            closed_at="2026-05-06T20:00:00+00:00",
            close_reason="TP atingido",
            result_pips=25.2,
            result_r_multiple=2.0,
        )

        row = memory_db.execute(
            "SELECT * FROM paper_trades WHERE id = ?", (paper_trade_id,)
        ).fetchone()
        assert row["status"] == "win"
        assert row["close_price"] == 1.17812
        assert row["result_pips"] == 25.2
        assert row["result_r_multiple"] == 2.0

    def test_paper_trades_summary_aggregates(self, memory_db):
        decision_id = database.save_decision(memory_db, _make_decision_entry())
        # cria 3 wins, 1 loss, 1 expired, 1 open
        statuses = [
            ("win", 25.0, 2.0),
            ("win", 30.0, 2.5),
            ("win", 20.0, 1.5),
            ("loss", -12.0, -1.0),
            ("expired", 5.0, 0.5),
            ("open", None, None),
        ]
        for status, pips, r in statuses:
            pt_id = database.create_paper_trade(memory_db, _make_paper_trade(decision_id, status=status))
            if pips is not None:
                database.update_paper_trade_result(
                    memory_db, pt_id, status, 1.18, "2026-05-06T20:00:00+00:00",
                    "x", pips, r,
                )

        summary = database.get_paper_trades_summary(memory_db)
        assert summary["total"] == 6
        assert summary["wins"] == 3
        assert summary["losses"] == 1
        assert summary["expired"] == 1
        assert summary["open"] == 1
        assert summary["win_rate"] == pytest.approx(75.0)  # 3 wins / 4 closed
        assert summary["best_pips"] == 30.0
        assert summary["worst_pips"] == -12.0

    def test_paper_trades_summary_empty(self, memory_db):
        summary = database.get_paper_trades_summary(memory_db)
        assert summary["total"] == 0
        assert summary["win_rate"] is None
        assert summary["avg_pips"] is None
        assert summary["best_pips"] is None

    def test_summary_filters_by_source(self, memory_db):
        decision_id = database.save_decision(memory_db, _make_decision_entry())
        ai_id = database.create_paper_trade(memory_db, _make_paper_trade(decision_id, source="ai_only"))
        comb_id = database.create_paper_trade(memory_db, _make_paper_trade(decision_id, source="combined"))

        database.update_paper_trade_result(memory_db, ai_id, "win", 1.18, "x", "x", 25.0, 2.0)
        database.update_paper_trade_result(memory_db, comb_id, "loss", 1.17, "x", "x", -12.0, -1.0)

        ai_summary = database.get_paper_trades_summary(memory_db, source="ai_only")
        assert ai_summary["wins"] == 1
        assert ai_summary["losses"] == 0

        comb_summary = database.get_paper_trades_summary(memory_db, source="combined")
        assert comb_summary["wins"] == 0
        assert comb_summary["losses"] == 1

    def test_calibration_summary_tracks_blocks_and_pips(self, memory_db):
        blocked_id = database.save_decision(
            memory_db,
            _make_decision_entry(
                trade_allowed=False,
                block_reason="outside_operational_trade_window",
                blocking_reason="outside_operational_trade_window",
                gating_confidence=40,
            ),
        )
        executed_id = database.save_decision(
            memory_db,
            _make_decision_entry(
                decision_signature="def456",
                trade_allowed=True,
                block_reason=None,
                blocking_reason="",
                combined_signal="BUY",
                gating_signal="BUY",
                gating_confidence=62,
            ),
        )
        assert blocked_id
        win_id = database.create_paper_trade(
            memory_db,
            _make_paper_trade(executed_id, source="combined", direction="BUY", status="win"),
        )
        loss_id = database.create_paper_trade(
            memory_db,
            _make_paper_trade(executed_id, source="combined", direction="SELL", status="loss"),
        )
        database.update_paper_trade_result(memory_db, win_id, "win", 1.18, "x", "x", 20.0, 2.0)
        database.update_paper_trade_result(memory_db, loss_id, "loss", 1.17, "x", "x", -10.0, -1.0)

        summary = database.get_calibration_summary(memory_db)

        assert summary["total_decisions"] == 2
        assert summary["total_blocked"] == 1
        assert summary["total_executed"] == 1
        assert summary["blocked_by_reason"]["outside_operational_trade_window"] == 1
        assert summary["wins"] == 1
        assert summary["losses"] == 1
        assert summary["winrate"] == 50.0
        assert summary["avg_confidence"] == 62.0
        assert summary["net_pips"] == 10.0
        assert summary["profit_factor"] == 2.0
        assert summary["expectancy"] == 5.0
        assert summary["best_direction"] == "BUY"


class TestMarketCandles:
    def test_get_between_returns_in_range(self, memory_db):
        candles = [
            {
                "candle_time": "2026-05-06T18:00:00+00:00",
                "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.05, "volume": 100,
            },
            {
                "candle_time": "2026-05-06T19:00:00+00:00",
                "open": 1.05, "high": 1.15, "low": 1.0, "close": 1.10, "volume": 100,
            },
            {
                "candle_time": "2026-05-06T20:00:00+00:00",
                "open": 1.10, "high": 1.20, "low": 1.05, "close": 1.15, "volume": 100,
            },
        ]
        database.save_market_candles(memory_db, candles, "EUR/USD", "1h", "yahoo")

        result = database.get_market_candles_between(
            memory_db, "EUR/USD", "1h",
            "2026-05-06T18:30:00+00:00", "2026-05-06T19:30:00+00:00",
        )
        assert len(result) == 1
        assert result[0]["candle_time"] == "2026-05-06T19:00:00+00:00"

    def test_get_between_filters_by_provider(self, memory_db):
        candle = {
            "candle_time": "2026-05-06T19:00:00+00:00",
            "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.05, "volume": 100,
        }
        database.save_market_candles(memory_db, [candle], "EUR/USD", "1h", "yahoo")
        database.save_market_candles(memory_db, [candle], "EUR/USD", "1h", "oanda")

        result = database.get_market_candles_between(
            memory_db, "EUR/USD", "1h",
            "2026-05-06T18:00:00+00:00", "2026-05-06T20:00:00+00:00",
            provider="yahoo",
        )
        assert len(result) == 1
