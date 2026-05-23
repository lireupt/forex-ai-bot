import pytest

import main
from modules import multi_timeframe, scoring


def _technical(score):
    return {
        "signal": scoring.score_to_signal(score),
        "confidence": int(abs(score) * 100),
        "indicators": {"technical_score": score},
    }


class TestMultiTimeframeAggregate:
    def test_all_timeframes_aligned_buy(self):
        result = multi_timeframe.aggregate({
            "m15": _technical(0.4),
            "h1": _technical(0.5),
            "h4": _technical(0.6),
            "d1": _technical(0.3),
        })
        assert result["multi_timeframe_score"] == pytest.approx(0.49)
        assert result["multi_timeframe_signal"] == "BUY"
        assert result["timeframe_block_reason"] == ""

    def test_all_timeframes_aligned_sell(self):
        result = multi_timeframe.aggregate({
            "m15": _technical(-0.4),
            "h1": _technical(-0.5),
            "h4": _technical(-0.6),
            "d1": _technical(-0.3),
        })
        assert result["multi_timeframe_score"] == pytest.approx(-0.49)
        assert result["multi_timeframe_signal"] == "SELL"
        assert result["timeframe_block_reason"] == ""

    def test_h1_buy_but_h4_and_d1_strongly_against_blocks(self):
        result = multi_timeframe.aggregate({
            "m15": _technical(0.2),
            "h1": _technical(0.7),
            "h4": _technical(-0.7),
            "d1": _technical(-0.8),
        })
        assert result["timeframe_alignment"] == "h4_d1_strongly_against_h1"
        assert result["timeframe_block_reason"] == "H4 e D1 fortemente contra H1"
        assert result["timeframe_confidence_adjustment"] == pytest.approx(0.25)

    def test_h1_buy_with_m15_neutral_does_not_block(self):
        result = multi_timeframe.aggregate({
            "m15": _technical(0.0),
            "h1": _technical(0.55),
            "h4": _technical(0.6),
            "d1": _technical(0.0),
        })
        assert result["multi_timeframe_signal"] == "BUY"
        assert result["timeframe_alignment"] == "h1_h4_aligned"
        assert result["timeframe_block_reason"] == ""

    def test_missing_timeframe_is_neutral_zero_only_for_that_timeframe(self):
        result = multi_timeframe.aggregate({
            "m15": _technical(0.5),
            "h1": _technical(0.5),
            "h4": _technical(0.5),
        })
        assert result["technical_score_d1"] == 0.0
        assert result["multi_timeframe_score"] == pytest.approx(0.45)
        assert result["multi_timeframe_signal"] == "BUY"


class TestFinalScoreWithMultiTimeframe:
    _CONFIG = {
        "buy_threshold": 0.35,
        "sell_threshold": -0.35,
        "technical_weight": 0.55,
        "ai_weight": 0.30,
        "news_weight": 0.15,
        "shadow_weight": 0.0,
    }

    def test_final_score_inside_neutral_zone(self, monkeypatch):
        monkeypatch.setenv("AI_VOTE_MIN_CONFIDENCE", "35")
        technical_result = {
            "signal": "NEUTRAL",
            "confidence": 0,
            "indicators": {"technical_score": 0.2},
            "multi_timeframe_score": 0.2,
        }
        ai_result = {"signal": "NEUTRAL", "confidence": 0, "hold_off": False}
        result = main._combine_signals(
            ai_result,
            technical_result,
            scoring_config=self._CONFIG,
            news_score=0.0,
        )
        # IA sem convicção (conf 0) abstém-se; a técnica 0.2 fica como combinado
        # e continua dentro da zona neutra.
        assert result["combined_score"] == pytest.approx(0.2)
        assert result["signal"] == "NEUTRAL"

    def test_confident_ai_aligned_reinforces_score(self, monkeypatch):
        monkeypatch.setenv("AI_VOTE_MIN_CONFIDENCE", "35")
        technical_result = {
            "signal": "BUY",
            "confidence": 60,
            "indicators": {"technical_score": 0.5},
            "multi_timeframe_score": 0.5,
        }
        ai_result = {
            "bias": "BUY",
            "confidence_adjustment": 0.12,
            "risk_adjustment": -0.05,
            "signal": "BUY",
            "confidence": 70,
            "hold_off": False,
        }
        result = main._combine_signals(
            ai_result,
            technical_result,
            scoring_config=self._CONFIG,
        )
        # IA confiante (70%) participa: (0.12*0.30 + 0.5*0.55) / 0.85 = 0.3659.
        assert result["components"]["ai_score"] == pytest.approx(0.12)
        assert result["combined_score"] == pytest.approx(0.3659)
        assert result["signal"] == "BUY"

    def test_low_confidence_ai_does_not_neutralise_clean_technical(self, monkeypatch):
        # Reproduz o caso de produção: técnica SELL -0.43 que antes era puxada
        # para -0.22 (NEUTRAL) por uma IA a 5%. Agora a IA abstém-se.
        monkeypatch.setenv("AI_VOTE_MIN_CONFIDENCE", "35")
        technical_result = {
            "signal": "SELL",
            "confidence": 43,
            "indicators": {"technical_score": -0.43},
            "multi_timeframe_score": -0.43,
        }
        ai_result = {"bias": "SELL", "signal": "SELL", "confidence": 5, "hold_off": False}
        result = main._combine_signals(
            ai_result,
            technical_result,
            scoring_config=self._CONFIG,
            news_score=0.0,
        )
        assert result["combined_score"] == pytest.approx(-0.43)
        assert result["signal"] == "SELL"

    def test_ai_bullish_with_technical_bearish_does_not_replace_technical(self, monkeypatch):
        monkeypatch.setenv("AI_VOTE_MIN_CONFIDENCE", "35")
        technical_result = {
            "signal": "SELL",
            "confidence": 60,
            "indicators": {"technical_score": -0.7},
            "multi_timeframe_score": -0.7,
        }
        ai_result = {
            "bias": "BUY",
            "confidence_adjustment": 0.12,
            "risk_adjustment": -0.05,
            "signal": "BUY",
            "confidence": 12,
            "hold_off": False,
        }
        result = main._combine_signals(
            ai_result,
            technical_result,
            scoring_config=self._CONFIG,
        )
        # IA contrária mas de baixa convicção (12%) abstém-se: SELL técnico intacto.
        assert result["combined_score"] == pytest.approx(-0.7)
        assert result["signal"] == "SELL"


class TestConfidenceAdjustments:
    def test_h1_h4_alignment_adds_confidence(self):
        adjustment, reasons = main._decision_confidence_adjustment({
            "timeframe_alignment": "h1_h4_aligned",
            "timeframe_block_reason": "",
        })
        assert adjustment == pytest.approx(0.10)
        assert "h1_h4_aligned:+0.10" in reasons

    def test_m15_against_h1_reduces_confidence(self):
        adjustment, reasons = main._decision_confidence_adjustment({
            "timeframe_alignment": "h1_h4_aligned_m15_against",
            "timeframe_block_reason": "",
        })
        assert adjustment == pytest.approx(0.0)
        assert "m15_against_h1:-0.10" in reasons

    def test_d1_strongly_against_h1_reduces_confidence(self):
        adjustment, reasons = main._decision_confidence_adjustment({
            "timeframe_alignment": "d1_strongly_against_h1",
            "timeframe_block_reason": "",
        })
        assert adjustment == pytest.approx(-0.20)
        assert "d1_strongly_against_h1:-0.20" in reasons
