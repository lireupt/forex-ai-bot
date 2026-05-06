"""Testes para `modules.scoring` — a matemática base é crítica e barata de testar."""

import pytest

from modules import scoring


class TestSignalScore:
    def test_buy_with_full_confidence(self):
        assert scoring.signal_score("BUY", 100) == 1.0

    def test_sell_with_full_confidence(self):
        assert scoring.signal_score("SELL", 100) == -1.0

    def test_neutral_is_zero_regardless_of_confidence(self):
        assert scoring.signal_score("NEUTRAL", 80) == 0.0
        assert scoring.signal_score("NEUTRAL", 0) == 0.0

    def test_buy_with_partial_confidence(self):
        assert scoring.signal_score("BUY", 60) == pytest.approx(0.6)

    def test_sell_with_partial_confidence(self):
        assert scoring.signal_score("SELL", 35) == pytest.approx(-0.35)

    def test_unknown_signal_treated_as_neutral(self):
        assert scoring.signal_score("MAYBE", 80) == 0.0

    def test_none_confidence_safe(self):
        assert scoring.signal_score("BUY", None) == 0.0

    def test_string_confidence_safe(self):
        assert scoring.signal_score("BUY", "garbage") == 0.0

    def test_confidence_above_100_clamped(self):
        assert scoring.signal_score("BUY", 250) == 1.0

    def test_negative_confidence_clamped(self):
        assert scoring.signal_score("BUY", -10) == 0.0


class TestTechnicalVotesScore:
    def test_three_bullish_max_positive(self):
        assert scoring.technical_votes_score("bullish", "bullish", "bullish") == pytest.approx(1.0)

    def test_three_bearish_max_negative(self):
        assert scoring.technical_votes_score("bearish", "bearish", "bearish") == pytest.approx(-1.0)

    def test_all_neutral_zero(self):
        assert scoring.technical_votes_score("neutral", "neutral", "neutral") == 0.0

    def test_two_bullish_one_neutral(self):
        result = scoring.technical_votes_score("bullish", "bullish", "neutral")
        assert result == pytest.approx(2 / 3)

    def test_one_bullish_one_bearish_cancels(self):
        result = scoring.technical_votes_score("bullish", "bearish", "neutral")
        assert result == 0.0

    def test_unknown_vote_treated_as_neutral(self):
        result = scoring.technical_votes_score("bullish", "weird", "bullish")
        assert result == pytest.approx(2 / 3)


class TestScoreToSignal:
    def test_buy_at_threshold(self):
        assert scoring.score_to_signal(0.35) == "BUY"

    def test_sell_at_threshold(self):
        assert scoring.score_to_signal(-0.35) == "SELL"

    def test_neutral_just_below_buy_threshold(self):
        assert scoring.score_to_signal(0.34) == "NEUTRAL"

    def test_neutral_just_above_sell_threshold(self):
        assert scoring.score_to_signal(-0.34) == "NEUTRAL"

    def test_none_returns_neutral(self):
        assert scoring.score_to_signal(None) == "NEUTRAL"

    def test_custom_thresholds_via_config(self):
        config = {
            "buy_threshold": 0.5,
            "sell_threshold": -0.5,
            "ai_weight": 0.6,
            "technical_weight": 0.4,
            "shadow_weight": 0.0,
        }
        assert scoring.score_to_signal(0.4, config) == "NEUTRAL"
        assert scoring.score_to_signal(0.5, config) == "BUY"

    def test_env_overrides_thresholds(self, monkeypatch):
        monkeypatch.setenv("SCORE_BUY_THRESHOLD", "0.7")
        monkeypatch.setenv("SCORE_SELL_THRESHOLD", "-0.7")
        # carregar config após mudar env
        config = scoring.load_scoring_config()
        assert scoring.score_to_signal(0.6, config) == "NEUTRAL"
        assert scoring.score_to_signal(0.71, config) == "BUY"


class TestCombineScores:
    def test_only_ai_returns_ai(self):
        config = {"ai_weight": 1.0, "technical_weight": 0.0, "shadow_weight": 0.0,
                  "buy_threshold": 0.35, "sell_threshold": -0.35}
        assert scoring.combine_scores(0.8, 0.0, config=config) == pytest.approx(0.8)

    def test_only_technical_returns_technical(self):
        config = {"ai_weight": 0.0, "technical_weight": 1.0, "shadow_weight": 0.0,
                  "buy_threshold": 0.35, "sell_threshold": -0.35}
        assert scoring.combine_scores(0.8, 0.5, config=config) == pytest.approx(0.5)

    def test_default_weights_60_40(self):
        # 0.6*0.6 + 0.4*0 = 0.36
        config = scoring.load_scoring_config()
        result = scoring.combine_scores(0.6, 0.0, config=config)
        assert result == pytest.approx(0.36)

    def test_opposite_directions_partially_cancel(self):
        # AI BUY 100% (1.0), técnica SELL 100% (-1.0), pesos 0.6/0.4
        config = scoring.load_scoring_config()
        result = scoring.combine_scores(1.0, -1.0, config=config)
        # 0.6*1.0 + 0.4*-1.0 = 0.2
        assert result == pytest.approx(0.2)

    def test_zero_weight_ignores_component(self):
        config = {"ai_weight": 0.0, "technical_weight": 0.0, "shadow_weight": 0.0,
                  "buy_threshold": 0.35, "sell_threshold": -0.35}
        assert scoring.combine_scores(1.0, 1.0, config=config) == 0.0

    def test_shadow_weight_used_when_positive(self):
        config = {"ai_weight": 0.5, "technical_weight": 0.0, "shadow_weight": 0.5,
                  "buy_threshold": 0.35, "sell_threshold": -0.35}
        result = scoring.combine_scores(0.4, 0.0, shadow_score=0.6, config=config)
        # weights normalizam para 0.5/0.5 sobre AI+shadow
        assert result == pytest.approx(0.5)

    def test_none_components_skipped(self):
        config = scoring.load_scoring_config()
        result = scoring.combine_scores(None, 0.5, config=config)
        # apenas técnica entra (peso 0.4 / total 0.4) -> 0.5
        assert result == pytest.approx(0.5)


class TestConfidenceToUnit:
    def test_zero(self):
        assert scoring.confidence_to_unit(0) == 0.0

    def test_full(self):
        assert scoring.confidence_to_unit(100) == 1.0

    def test_partial(self):
        assert scoring.confidence_to_unit(75) == 0.75

    def test_clamped_above(self):
        assert scoring.confidence_to_unit(150) == 1.0

    def test_clamped_below(self):
        assert scoring.confidence_to_unit(-10) == 0.0

    def test_none_safe(self):
        assert scoring.confidence_to_unit(None) == 0.0

    def test_string_safe(self):
        assert scoring.confidence_to_unit("invalid") == 0.0


class TestLoadScoringConfig:
    def test_defaults(self):
        config = scoring.load_scoring_config()
        assert config["buy_threshold"] == 0.35
        assert config["sell_threshold"] == -0.35
        assert config["ai_weight"] == 0.6
        assert config["technical_weight"] == 0.4
        assert config["shadow_weight"] == 0.0

    def test_invalid_env_value_falls_back(self, monkeypatch):
        monkeypatch.setenv("SCORE_BUY_THRESHOLD", "not_a_number")
        config = scoring.load_scoring_config()
        assert config["buy_threshold"] == 0.35
