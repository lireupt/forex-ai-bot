"""Testes para `modules.candlestick_patterns` — detecção de padrões shadow (peso 0)."""

import pytest

from modules import candlestick_patterns as cp


def _d1_series(values):
    return [{"close": v} for v in values]


def _linear_d1(start, end, count=60):
    step = (end - start) / (count - 1)
    return _d1_series([start + step * i for i in range(count)])


BULLISH_D1 = _linear_d1(1.0800, 1.1200)
BEARISH_D1 = _linear_d1(1.1200, 1.0800)
NEUTRAL_D1 = _d1_series([1.1000] * 60)


class TestBullishEngulfing:
    def _pair(self, current_close):
        previous = {"open": 1.1050, "close": 1.1020, "high": 1.1055, "low": 1.1015}
        current = {"open": 1.1015, "close": current_close, "high": 1.1065, "low": 1.1010}
        return [previous, current]

    def test_full_engulfment_detected(self):
        result = cp.detect_patterns(self._pair(1.1060))
        assert "bullish_engulfing" in result.pattern_names
        assert result.raw_score == pytest.approx(cp.ENGULFING_SCORE)

    def test_fails_by_one_pip_not_detected(self):
        # current.close = 1.1049 fica 1 pip abaixo do previous.open (1.1050)
        result = cp.detect_patterns(self._pair(1.1049))
        assert "bullish_engulfing" not in result.pattern_names
        assert result.pattern_score == 0.0


class TestBearishEngulfing:
    def _pair(self, current_close):
        previous = {"open": 1.1020, "close": 1.1050, "high": 1.1055, "low": 1.1015}
        current = {"open": 1.1060, "close": current_close, "high": 1.1065, "low": 1.1005}
        return [previous, current]

    def test_full_engulfment_detected(self):
        result = cp.detect_patterns(self._pair(1.1010))
        assert "bearish_engulfing" in result.pattern_names
        assert result.raw_score == pytest.approx(-cp.ENGULFING_SCORE)

    def test_fails_by_one_pip_not_detected(self):
        # current.close = 1.1021 fica 1 pip acima do previous.open (1.1020)
        result = cp.detect_patterns(self._pair(1.1021))
        assert "bearish_engulfing" not in result.pattern_names
        assert result.pattern_score == 0.0


class TestHammerShootingStar:
    def _candles(self, current):
        previous = {"open": 1.1025, "high": 1.1028, "low": 1.1022, "close": 1.1023}
        return [previous, current]

    def test_hammer_detected(self):
        current = {"open": 1.1030, "close": 1.1034, "high": 1.1040, "low": 1.1000}
        result = cp.detect_patterns(self._candles(current))
        assert "hammer" in result.pattern_names
        assert result.raw_score == pytest.approx(cp.PIN_BAR_SCORE)

    def test_shooting_star_detected(self):
        current = {"open": 1.1010, "close": 1.1006, "high": 1.1040, "low": 1.1000}
        result = cp.detect_patterns(self._candles(current))
        assert "shooting_star" in result.pattern_names
        assert result.raw_score == pytest.approx(-cp.PIN_BAR_SCORE)

    def test_shadow_ratio_below_threshold_not_detected(self):
        # lower_shadow = 0.0007 < 2x body (0.0008) -> não qualifica como hammer
        current = {"open": 1.1030, "close": 1.1034, "high": 1.1040, "low": 1.1023}
        result = cp.detect_patterns(self._candles(current))
        assert "hammer" not in result.pattern_names
        assert "shooting_star" not in result.pattern_names
        assert result.pattern_score == 0.0


class TestInsideBar:
    def test_contained_range_detected(self):
        previous = {"open": 1.1010, "close": 1.1040, "high": 1.1050, "low": 1.1000}
        current = {"open": 1.1020, "close": 1.1025, "high": 1.1040, "low": 1.1010}
        result = cp.detect_patterns([previous, current])
        assert "inside_bar" in result.pattern_names
        # inside_bar não contribui para o score (contexto, não sinal)
        assert result.raw_score == 0.0
        assert result.pattern_score == 0.0

    def test_not_contained_not_detected(self):
        previous = {"open": 1.1010, "close": 1.1040, "high": 1.1050, "low": 1.1000}
        current = {"open": 1.1020, "close": 1.1025, "high": 1.1055, "low": 1.1010}
        result = cp.detect_patterns([previous, current])
        assert "inside_bar" not in result.pattern_names


class TestDoji:
    def test_small_body_detected(self):
        previous = {"open": 1.1015, "close": 1.1020, "high": 1.1025, "low": 1.1005}
        current = {"open": 1.1020, "close": 1.1021, "high": 1.1030, "low": 1.1010}
        result = cp.detect_patterns([previous, current])
        assert "doji" in result.pattern_names
        assert result.raw_score == 0.0
        assert result.pattern_score == 0.0

    def test_large_body_not_doji(self):
        previous = {"open": 1.1015, "close": 1.1020, "high": 1.1025, "low": 1.1005}
        current = {"open": 1.1000, "close": 1.1040, "high": 1.1042, "low": 1.0998}
        result = cp.detect_patterns([previous, current])
        assert "doji" not in result.pattern_names


class TestD1TrendMultiplier:
    def _bullish_engulfing_pair(self):
        previous = {"open": 1.1050, "close": 1.1020, "high": 1.1055, "low": 1.1015}
        current = {"open": 1.1015, "close": 1.1060, "high": 1.1065, "low": 1.1010}
        return [previous, current]

    def test_aligned_with_d1_uses_full_multiplier(self):
        result = cp.detect_patterns(self._bullish_engulfing_pair(), d1_candles=BULLISH_D1)
        assert result.d1_trend == "bullish"
        assert result.pattern_score == pytest.approx(cp.ENGULFING_SCORE * cp.D1_ALIGNED_MULTIPLIER)

    def test_against_d1_uses_reduced_multiplier(self):
        result = cp.detect_patterns(self._bullish_engulfing_pair(), d1_candles=BEARISH_D1)
        assert result.d1_trend == "bearish"
        assert result.pattern_score == pytest.approx(cp.ENGULFING_SCORE * cp.D1_AGAINST_MULTIPLIER)

    def test_neutral_d1_uses_half_multiplier(self):
        result = cp.detect_patterns(self._bullish_engulfing_pair(), d1_candles=NEUTRAL_D1)
        assert result.d1_trend == "neutral"
        assert result.pattern_score == pytest.approx(cp.ENGULFING_SCORE * cp.D1_NEUTRAL_MULTIPLIER)

    def test_missing_d1_candles_uses_half_multiplier(self):
        result = cp.detect_patterns(self._bullish_engulfing_pair(), d1_candles=None)
        assert result.d1_trend == "neutral"
        assert result.pattern_score == pytest.approx(cp.ENGULFING_SCORE * cp.D1_NEUTRAL_MULTIPLIER)

    def test_insufficient_d1_candles_treated_as_neutral(self):
        result = cp.detect_patterns(self._bullish_engulfing_pair(), d1_candles=BULLISH_D1[:10])
        assert result.d1_trend == "neutral"


class TestMultiplePatternsClamp:
    def _dual_pattern_pair(self):
        # candle 'current' qualifica simultaneamente como bullish_engulfing
        # (contra o 'previous' de corpo minúsculo) e como hammer (corpo
        # pequeno, sombra inferior dominante) — soma de raw contributions.
        previous = {"open": 1.1021, "close": 1.1020, "high": 1.1022, "low": 1.1019}
        current = {"open": 1.1020, "close": 1.1021, "high": 1.1024, "low": 1.1000}
        return [previous, current]

    def test_directional_contributions_sum(self):
        result = cp.detect_patterns(self._dual_pattern_pair())
        assert "bullish_engulfing" in result.pattern_names
        assert "hammer" in result.pattern_names
        assert result.raw_score == pytest.approx(cp.ENGULFING_SCORE + cp.PIN_BAR_SCORE)

    def test_clamped_to_one_when_sum_exceeds_bound(self, monkeypatch):
        monkeypatch.setattr(cp, "ENGULFING_SCORE", 0.8)
        result = cp.detect_patterns(self._dual_pattern_pair(), d1_candles=BULLISH_D1)
        assert result.raw_score == 1.0
        assert result.pattern_score == 1.0


class TestEmptyOrInsufficientCandles:
    def test_empty_list_returns_zero_without_exception(self):
        result = cp.detect_patterns([])
        assert result.pattern_score == 0.0
        assert result.pattern_names == []

    def test_single_candle_returns_zero_without_exception(self):
        result = cp.detect_patterns([{"open": 1.1, "close": 1.1005, "high": 1.101, "low": 1.099}])
        assert result.pattern_score == 0.0
        assert result.pattern_names == []

    def test_none_candles_returns_zero_without_exception(self):
        result = cp.detect_patterns(None)
        assert result.pattern_score == 0.0
