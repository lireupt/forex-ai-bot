"""Testes para `modules.trade_simulator` — resolução pura de trades."""

from datetime import datetime, timezone

import pytest

from modules.pair_spec import get_pair_spec
from modules.trade_simulator import TradeResult, compute_r_multiple, signed_pips, simulate_trade

EURUSD = get_pair_spec("EUR/USD")


def _candle(time, high, low, close, open_=None):
    return {
        "candle_time": time,
        "open": open_ if open_ is not None else low,
        "high": high,
        "low": low,
        "close": close,
        "volume": 0,
    }


def _trade(**overrides):
    base = {
        "direction": "BUY",
        "entry_price": 1.1700,
        "simulated_sl": 1.1680,
        "simulated_tp": 1.1740,
        "expiry_at": "2026-05-07T01:00:00+00:00",
    }
    base.update(overrides)
    return base


class TestSignedPipsAndRMultiple:
    def test_signed_pips_buy_profit(self):
        assert signed_pips("BUY", 1.1700, 1.1750, EURUSD.pip_size) == 50.0

    def test_signed_pips_sell_profit(self):
        assert signed_pips("SELL", 1.1700, 1.1680, EURUSD.pip_size) == 20.0

    def test_r_multiple_zero_risk_is_none(self):
        assert compute_r_multiple("BUY", 1.17, 1.18, 1.17) is None


class TestWorstCaseSameCandle:
    def test_buy_tp_and_sl_in_same_candle_assumes_sl_first(self):
        candles = [_candle("2026-05-06T20:00:00+00:00", 1.1750, 1.1675, 1.1700)]
        result = simulate_trade(_trade(), candles, EURUSD)
        assert result.status == "loss"
        assert result.close_price == 1.1680
        assert "SL e TP" in result.close_reason

    def test_sell_tp_and_sl_in_same_candle_assumes_sl_first(self):
        trade = _trade(direction="SELL", entry_price=1.1700, simulated_sl=1.1720, simulated_tp=1.1660)
        candles = [_candle("2026-05-06T20:00:00+00:00", 1.1725, 1.1655, 1.1700)]
        result = simulate_trade(trade, candles, EURUSD)
        assert result.status == "loss"
        assert result.close_price == 1.1720

    def test_result_is_trade_result_dataclass(self):
        candles = [_candle("2026-05-06T20:00:00+00:00", 1.1750, 1.1690, 1.1740)]
        result = simulate_trade(_trade(), candles, EURUSD)
        assert isinstance(result, TradeResult)


class TestSpreadAdjustment:
    def test_spread_disabled_by_default_matches_raw_entry(self):
        candles = [_candle("2026-05-06T20:00:00+00:00", 1.1750, 1.1715, 1.1740)]
        result = simulate_trade(_trade(), candles, EURUSD)
        assert result.result_pips == 40.0

    def test_spread_enabled_worsens_buy_entry(self):
        # 1 pip de spread (EURUSD.spread_pips=1.0) -> entry efetivo 1.1701
        candles = [_candle("2026-05-06T20:00:00+00:00", 1.1750, 1.1715, 1.1740)]
        result = simulate_trade(_trade(), candles, EURUSD, apply_spread=True)
        assert result.result_pips == pytest.approx(39.0)

    def test_spread_enabled_worsens_sell_entry(self):
        trade = _trade(direction="SELL", entry_price=1.1700, simulated_sl=1.1720, simulated_tp=1.1660)
        candles = [_candle("2026-05-06T20:00:00+00:00", 1.1710, 1.1655, 1.1670)]
        result = simulate_trade(trade, candles, EURUSD, apply_spread=True)
        # entry efetivo 1.1699 (SELL: -1 pip), tp em 1.1660 -> 39 pips em vez de 40
        assert result.result_pips == pytest.approx(39.0)

    def test_spread_does_not_change_sl_tp_touch_thresholds(self):
        # SL/TP são preços absolutos já fixados pelo risk engine; o spread
        # só desloca o custo de entrada, não os níveis de SL/TP.
        candles = [_candle("2026-05-06T20:00:00+00:00", 1.1740, 1.1715, 1.1730)]
        no_spread = simulate_trade(_trade(), candles, EURUSD, apply_spread=False)
        with_spread = simulate_trade(_trade(), candles, EURUSD, apply_spread=True)
        assert no_spread.status == with_spread.status == "win"
        assert no_spread.close_price == with_spread.close_price == 1.1740


class TestExpiryPointInTime:
    def test_not_expired_relative_to_injected_now(self):
        candles = [_candle("2026-05-06T20:00:00+00:00", 1.1710, 1.1690, 1.1705)]
        trade = _trade(expiry_at="2026-05-07T01:00:00+00:00")
        now = datetime(2026, 5, 6, 21, 0, tzinfo=timezone.utc)
        assert simulate_trade(trade, candles, EURUSD, now_dt=now) is None

    def test_expired_relative_to_injected_now(self):
        candles = [_candle("2026-05-06T20:00:00+00:00", 1.1710, 1.1695, 1.1705)]
        trade = _trade(expiry_at="2026-05-07T01:00:00+00:00")
        now = datetime(2026, 5, 7, 2, 0, tzinfo=timezone.utc)
        result = simulate_trade(trade, candles, EURUSD, now_dt=now)
        assert result.status == "expired"
        assert result.close_price == 1.1705
