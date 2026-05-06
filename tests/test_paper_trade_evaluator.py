"""Testes para `scripts.evaluate_paper_trades` — TP/SL/expiry detection."""

import pytest

from scripts import evaluate_paper_trades as ev


def _candle(time, high, low, close, open_=None):
    return {
        "candle_time": time,
        "open": open_ if open_ is not None else low,
        "high": high,
        "low": low,
        "close": close,
        "volume": 0,
    }


class TestSignedPips:
    def test_buy_profit(self):
        assert ev._signed_pips("BUY", 1.1700, 1.1750) == 50.0

    def test_buy_loss(self):
        assert ev._signed_pips("BUY", 1.1700, 1.1680) == -20.0

    def test_sell_profit(self):
        assert ev._signed_pips("SELL", 1.1700, 1.1680) == 20.0

    def test_sell_loss(self):
        assert ev._signed_pips("SELL", 1.1700, 1.1730) == -30.0


class TestRMultiple:
    def test_buy_win_2r(self):
        # entry 1.17, sl 1.165 (50 pips risk), exit 1.18 (100 pips reward) -> 2R
        result = ev._compute_r_multiple("BUY", 1.17, 1.18, 1.165)
        assert result == pytest.approx(2.0)

    def test_buy_loss_minus_1r(self):
        # entry 1.17, sl 1.165, exit at sl -> -1R
        result = ev._compute_r_multiple("BUY", 1.17, 1.165, 1.165)
        assert result == pytest.approx(-1.0)

    def test_sell_win_2r(self):
        # entry 1.17, sl 1.175 (50 pips), exit 1.16 (100 pips) -> 2R
        result = ev._compute_r_multiple("SELL", 1.17, 1.16, 1.175)
        assert result == pytest.approx(2.0)

    def test_zero_risk_returns_none(self):
        assert ev._compute_r_multiple("BUY", 1.17, 1.18, 1.17) is None


class TestEvaluateTrade:
    def _trade(self, **overrides):
        base = {
            "id": 1,
            "direction": "BUY",
            "entry_price": 1.1700,
            "simulated_sl": 1.1680,  # 20 pips abaixo
            "simulated_tp": 1.1740,  # 40 pips acima
            "expiry_at": "2026-05-07T01:00:00+00:00",
        }
        base.update(overrides)
        return base

    def test_buy_tp_hit(self):
        candles = [
            _candle("2026-05-06T20:00:00+00:00", 1.1730, 1.1690, 1.1710),
            _candle("2026-05-06T21:00:00+00:00", 1.1750, 1.1715, 1.1740),
        ]
        result = ev._evaluate_trade(self._trade(), candles)
        assert result["status"] == "win"
        assert result["close_price"] == 1.1740
        assert result["result_pips"] == 40.0
        assert result["result_r_multiple"] == pytest.approx(2.0)

    def test_buy_sl_hit(self):
        candles = [
            _candle("2026-05-06T20:00:00+00:00", 1.1710, 1.1675, 1.1685),
        ]
        result = ev._evaluate_trade(self._trade(), candles)
        assert result["status"] == "loss"
        assert result["close_price"] == 1.1680
        assert result["result_pips"] == -20.0

    def test_sell_tp_hit(self):
        trade = self._trade(direction="SELL", entry_price=1.1700,
                            simulated_sl=1.1720, simulated_tp=1.1660)
        candles = [
            _candle("2026-05-06T20:00:00+00:00", 1.1710, 1.1655, 1.1670),
        ]
        result = ev._evaluate_trade(trade, candles)
        assert result["status"] == "win"
        assert result["close_price"] == 1.1660

    def test_sell_sl_hit(self):
        trade = self._trade(direction="SELL", entry_price=1.1700,
                            simulated_sl=1.1720, simulated_tp=1.1660)
        candles = [
            _candle("2026-05-06T20:00:00+00:00", 1.1725, 1.1690, 1.1715),
        ]
        result = ev._evaluate_trade(trade, candles)
        assert result["status"] == "loss"
        assert result["close_price"] == 1.1720

    def test_both_hit_same_candle_treats_as_loss(self):
        # candle alta toca TP e SL -> conservador, marca loss
        candles = [
            _candle("2026-05-06T20:00:00+00:00", 1.1750, 1.1675, 1.1700),
        ]
        result = ev._evaluate_trade(self._trade(), candles)
        assert result["status"] == "loss"
        assert "SL e TP" in result["close_reason"]

    def test_no_hit_returns_none_when_not_expired(self, monkeypatch):
        # expiry no futuro
        from datetime import datetime, timezone
        future = "2099-01-01T00:00:00+00:00"
        candles = [
            _candle("2026-05-06T20:00:00+00:00", 1.1710, 1.1690, 1.1705),
        ]
        result = ev._evaluate_trade(self._trade(expiry_at=future), candles)
        assert result is None

    def test_expired_when_no_hit_and_past_expiry(self, monkeypatch):
        # expiry no passado
        candles = [
            _candle("2026-05-06T20:00:00+00:00", 1.1710, 1.1695, 1.1705),
        ]
        result = ev._evaluate_trade(self._trade(expiry_at="2020-01-01T00:00:00+00:00"), candles)
        assert result["status"] == "expired"
        assert result["close_price"] == 1.1705
        # 5 pips acima da entrada (1.17 -> 1.1705)
        assert result["result_pips"] == 5.0

    def test_expired_with_no_candles_uses_entry(self):
        result = ev._evaluate_trade(self._trade(expiry_at="2020-01-01T00:00:00+00:00"), [])
        assert result["status"] == "expired"
        assert result["close_price"] == 1.17

    def test_invalid_candle_time_skipped(self):
        candles = [
            _candle("not_a_date", 1.1750, 1.1690, 1.1740),  # ignorada
            _candle("2026-05-06T20:00:00+00:00", 1.1750, 1.1715, 1.1745),
        ]
        result = ev._evaluate_trade(self._trade(), candles)
        # apenas a 2ª candle conta -> TP atingido
        assert result["status"] == "win"


class TestEvaluateIntegration:
    """Teste de integração: usa a DB temporária de `memory_db`, cria paper
    trade e candles e corre `evaluate()` end-to-end."""

    def test_evaluator_closes_open_trade_on_tp(self, memory_db, monkeypatch):
        from modules import database

        # decisão dummy
        decision_id = memory_db.execute(
            "INSERT INTO decisions (timestamp, pair, created_at) VALUES (?, ?, ?)",
            ("2026-05-06T19:00:00+00:00", "EUR/USD", "2026-05-06T19:00:00+00:00"),
        ).lastrowid
        memory_db.commit()

        paper_trade_id = database.create_paper_trade(memory_db, {
            "decision_id": decision_id,
            "pair": "EUR/USD",
            "timeframe": "1h",
            "direction": "BUY",
            "entry_price": 1.1700,
            "simulated_sl": 1.1680,
            "simulated_tp": 1.1740,
            "sl_pips": 20.0, "tp_pips": 40.0,
            "atr_pips": 20.0, "atr_price": 0.0020,
            "status": "open",
            "source": "ai_only",
            "signal_source": "ai_signal",
            "created_at": "2026-05-06T19:00:00+00:00",
            "expiry_at": "2026-05-07T01:00:00+00:00",
        })

        # candle que toca TP
        database.save_market_candles(
            memory_db,
            [{
                "candle_time": "2026-05-06T20:00:00+00:00",
                "open": 1.1700, "high": 1.1745, "low": 1.1690, "close": 1.1740,
                "volume": 100,
            }],
            "EUR/USD", "1h", "yahoo",
        )

        # Patch database.connect para devolver a memory_db (não fechar)
        class _NoCloseConn:
            def __init__(self, conn):
                self._conn = conn
            def __getattr__(self, name):
                return getattr(self._conn, name)
            def close(self):
                pass

        monkeypatch.setattr(database, "connect", lambda: _NoCloseConn(memory_db))

        stats = ev.evaluate(pair="EUR/USD")
        assert stats["updated"] == 1

        row = memory_db.execute(
            "SELECT status, close_price, result_pips FROM paper_trades WHERE id = ?",
            (paper_trade_id,),
        ).fetchone()
        assert row["status"] == "win"
        assert row["close_price"] == 1.174
        assert row["result_pips"] == 40.0
