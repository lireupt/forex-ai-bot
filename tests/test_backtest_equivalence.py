"""Testes para `scripts.backtest_equivalence` — matching e comparação pura."""

from datetime import datetime, timedelta, timezone

import pytest

from scripts import backtest_equivalence as eq

PIP = 0.0001


def _bt_trade(**overrides):
    base = {
        "created_at": "2026-06-30T09:00:00+00:00",
        "direction": "BUY",
        "entry_price": 1.1000,
        "simulated_sl": 1.0980,
        "simulated_tp": 1.1040,
        "status": "win",
    }
    base.update(overrides)
    return base


def _live_trade(**overrides):
    base = {
        "created_at": "2026-06-30T09:00:00+00:00",
        "direction": "BUY",
        "entry_price": 1.1000,
        "simulated_sl": 1.0980,
        "simulated_tp": 1.1040,
        "status": "win",
    }
    base.update(overrides)
    return base


class TestPipDiff:
    def test_none_values_return_none(self):
        assert eq._pip_diff(None, 1.1, PIP) is None
        assert eq._pip_diff(1.1, None, PIP) is None

    def test_diff_in_pips(self):
        assert eq._pip_diff(1.1000, 1.1005, PIP) == pytest.approx(5.0)


class TestCompareTrades:
    def test_exact_match_within_tolerance(self):
        result = eq.compare_trades([_bt_trade()], [_live_trade()], PIP)
        assert len(result["matched"]) == 1
        assert result["matched"][0]["within_tolerance"] is True
        assert not result["unmatched_backtest"]
        assert not result["unmatched_live"]

    def test_small_price_diff_within_half_pip_tolerance(self):
        bt = _bt_trade(entry_price=1.10003)  # 0.3 pip de diferença
        result = eq.compare_trades([bt], [_live_trade()], PIP)
        assert result["matched"][0]["within_tolerance"] is True

    def test_price_diff_above_tolerance_flagged(self):
        bt = _bt_trade(entry_price=1.1010)  # 10 pips de diferença
        result = eq.compare_trades([bt], [_live_trade()], PIP)
        assert result["matched"][0]["within_tolerance"] is False
        assert result["matched"][0]["entry_diff_pips"] == pytest.approx(10.0)

    def test_result_mismatch_flagged_even_with_identical_prices(self):
        bt = _bt_trade(status="loss")
        result = eq.compare_trades([bt], [_live_trade(status="win")], PIP)
        assert result["matched"][0]["result_match"] is False
        assert result["matched"][0]["within_tolerance"] is False

    def test_different_direction_not_paired(self):
        bt = _bt_trade(direction="BUY")
        live = _live_trade(direction="SELL")
        result = eq.compare_trades([bt], [live], PIP)
        assert result["matched"] == []
        assert result["unmatched_backtest"] == [bt]
        assert result["unmatched_live"] == [live]

    def test_outside_time_window_not_paired(self):
        bt = _bt_trade(created_at="2026-06-30T09:00:00+00:00")
        live = _live_trade(created_at="2026-06-30T09:30:00+00:00")  # 30 min > janela de 10
        result = eq.compare_trades([bt], [live], PIP)
        assert result["matched"] == []

    def test_picks_closest_candidate_when_multiple_within_window(self):
        bt = _bt_trade(created_at="2026-06-30T09:05:00+00:00")
        closer = _live_trade(created_at="2026-06-30T09:04:00+00:00", entry_price=1.1001)
        farther = _live_trade(created_at="2026-06-30T09:09:00+00:00", entry_price=1.1050)
        result = eq.compare_trades([bt], [farther, closer], PIP)
        assert result["matched"][0]["live_trade"] is closer


class TestBuildAiResultLookup:
    def test_lookup_reconstructs_ai_result_from_stored_decision(self, memory_db):
        memory_db.execute(
            """
            INSERT INTO decisions
            (timestamp, created_at, pair, ai_signal, ai_bias, ai_confidence_score,
             ai_confidence_adjustment, ai_risk_adjustment, hold_off, ai_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "2026-06-30T09:02:00+00:00", "2026-06-30T09:02:00+00:00", "EUR/USD",
                "BUY", "BUY", 0.72, 0.15, 0.0, 0, "ok",
            ),
        )
        memory_db.commit()

        lookup = eq.build_ai_result_lookup(
            memory_db, "EUR/USD", "2026-06-30T00:00:00+00:00", "2026-07-01T00:00:00+00:00",
        )
        ai_result = lookup("2026-06-30T09:00:00+00:00")
        assert ai_result is not None
        assert ai_result["signal"] == "BUY"
        assert ai_result["confidence"] == 72
        assert ai_result["confidence_adjustment"] == pytest.approx(0.15)

    def test_lookup_returns_none_outside_match_window(self, memory_db):
        memory_db.execute(
            """
            INSERT INTO decisions
            (timestamp, created_at, pair, ai_signal, ai_confidence_score)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("2026-06-30T09:00:00+00:00", "2026-06-30T09:00:00+00:00", "EUR/USD", "BUY", 0.5),
        )
        memory_db.commit()

        lookup = eq.build_ai_result_lookup(
            memory_db, "EUR/USD", "2026-06-30T00:00:00+00:00", "2026-07-01T00:00:00+00:00",
        )
        assert lookup("2026-06-30T11:00:00+00:00") is None


class TestRunEquivalenceSmoke:
    def test_level_b_runs_end_to_end_without_crashing(self, memory_db):
        from modules import database

        start = datetime(2026, 6, 30, tzinfo=timezone.utc)
        candles = []
        price = 1.1000
        for i in range(60):
            price = round(price + 0.00005 * ((i % 5) - 2), 5)
            candles.append({
                "candle_time": (start + timedelta(hours=i)).isoformat(),
                "open": price, "high": price + 0.0006, "low": price - 0.0006,
                "close": price, "volume": 100.0,
            })
        database.save_market_candles(memory_db, candles, "EUR/USD", "1h", "import")
        memory_db.commit()

        date_from = (start + timedelta(hours=40)).isoformat()
        date_to = (start + timedelta(hours=50)).isoformat()

        stats, comparison = eq.run_equivalence("EUR/USD", date_from, date_to, level="b")
        assert stats["total_decisions"] == 11
        assert isinstance(comparison["matched"], list)
