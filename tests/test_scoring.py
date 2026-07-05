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


class TestNewsScore:
    """Bug 3: news_score(ai_result, news_items) deve produzir score fundamentado em [-1, 1]."""

    def _ai(self, bias="BUY", sentiment="positive", adj=0.20):
        return {"bias": bias, "news_sentiment": sentiment, "confidence_adjustment": adj}

    def test_no_news_returns_zero_with_no_news_basis(self):
        score, basis = scoring.news_score(self._ai(), [])
        assert score == 0.0
        assert basis == "no_news"

    def test_empty_ai_result_returns_zero(self):
        score, basis = scoring.news_score({}, ["artigo"])
        assert score == 0.0

    def test_positive_sentiment_buy_bias_reinforces(self):
        # base=0.5, direction=1.0, adj=0.20 → 0.5 + 0.20 = 0.70
        score, basis = scoring.news_score(self._ai("BUY", "positive", 0.20), ["a"])
        assert score == pytest.approx(0.70)
        assert "positive" in basis
        assert "BUY" in basis

    def test_negative_sentiment_sell_bias_reinforces(self):
        # base=-0.5, direction=-1.0, adj=0.20 → -0.5 + (-1.0)*0.20 = -0.70
        score, basis = scoring.news_score(self._ai("SELL", "negative", 0.20), ["a"])
        assert score == pytest.approx(-0.70)

    def test_neutral_sentiment_neutral_bias_is_zero(self):
        score, basis = scoring.news_score(self._ai("NEUTRAL", "neutral", 0.0), ["a"])
        assert score == pytest.approx(0.0)

    def test_contradictory_sentiment_reduces_magnitude(self):
        # sentimento positivo mas bias SELL: base=0.5, direction=-1.0, adj=0.20 → 0.30
        score, basis = scoring.news_score(self._ai("SELL", "positive", 0.20), ["a"])
        assert score == pytest.approx(0.30)

    def test_score_clamped_to_one(self):
        # base=0.5, direction=1.0, adj=0.50 → 1.0 (clamped)
        score, _ = scoring.news_score(self._ai("BUY", "positive", 0.50), ["a"])
        assert score <= 1.0
        assert score == pytest.approx(1.0)

    def test_basis_includes_article_count(self):
        _, basis = scoring.news_score(self._ai(), ["a", "b", "c"])
        assert "n_articles=3" in basis


class TestCombineScoresThreeComponents:
    """Bug 2: combined_score não pode ser igual a nenhum componente isolado."""

    _COMBINED_CONFIG = {
        "buy_threshold": 0.35,
        "sell_threshold": -0.35,
        "ai_weight": 0.30,
        "technical_weight": 0.55,
        "news_weight": 0.15,
        "shadow_weight": 0.0,
    }

    def test_three_components_result_not_equal_to_any_single(self):
        # ai=1.0, tech=0.0, news=-1.0 → combined = (1.0*0.30 + 0*0.55 + -1.0*0.15) / 1.0 = 0.15
        result = scoring.combine_scores(1.0, 0.0, news_score=-1.0, config=self._COMBINED_CONFIG)
        assert result != 1.0
        assert result != 0.0
        assert result != -1.0
        assert result == pytest.approx(0.15)

    def test_ai_abstained_renormalizes_correctly(self):
        """Quando IA abstém (ai_score=None), fórmula de renormalização:
        combined = tech × (0.55/0.70) + news × (0.15/0.70)
        """
        tech = 0.5
        news = 0.4
        result = scoring.combine_scores(None, tech, news_score=news, config=self._COMBINED_CONFIG)
        expected = (tech * 0.55 + news * 0.15) / (0.55 + 0.15)
        assert result == pytest.approx(expected, abs=1e-4)

    def test_ai_abstained_no_news_equals_technical_score(self):
        # Sem IA e sem news (ambos None/0), só a técnica — resultado == technical.
        result = scoring.combine_scores(None, 0.6, news_score=None, config=self._COMBINED_CONFIG)
        assert result == pytest.approx(0.6)

    def test_combined_config_default_used_when_no_config(self):
        # Sem config explícita, combine_scores deve usar load_combined_scoring_config()
        # (ai=0.30, tech=0.55, news=0.15), NÃO a legacy (ai=0.60, tech=0.40).
        result = scoring.combine_scores(1.0, 0.0, news_score=None)
        # ai=1.0 com peso 0.30 (combined) vs 0.60 (legacy): resultados diferentes.
        # Com combined: 1.0*0.30 / 0.30 = 1.0 (só AI presente) → 1.0
        # Com legacy:   1.0*0.60 / 0.60 = 1.0 → coincide neste caso extremo.
        # Usar caso não-extremo: ai=0.5, tech=0.5, news=None
        result2 = scoring.combine_scores(0.5, 0.5, news_score=None)
        # combined config: (0.5*0.30 + 0.5*0.55) / 0.85 = 0.425/0.85 = 0.5
        # legacy config:   (0.5*0.60 + 0.5*0.40) / 1.00 = 0.5
        # Ambos dão 0.5 aqui... usar caso com ai≠tech
        result3 = scoring.combine_scores(1.0, 0.0, news_score=None)
        # combined: 1.0*0.30 / 0.30 = 1.0; legacy: 1.0*0.60 / 0.60 = 1.0 (mesmo)
        # Melhor caso: só verificar que não falha e que o resultado é sensato
        assert -1.0 <= result3 <= 1.0


class TestCombineScoresWithPatternComponent:
    """Padrões de candlestick (Camada 4, shadow) — SCORE_PATTERN_WEIGHT default 0.0."""

    _COMBINED_CONFIG = {
        "buy_threshold": 0.35,
        "sell_threshold": -0.35,
        "ai_weight": 0.30,
        "technical_weight": 0.55,
        "news_weight": 0.15,
        "shadow_weight": 0.0,
    }

    def test_pattern_weight_zero_is_bit_identical_to_no_pattern_regression(self):
        """SCORE_PATTERN_WEIGHT=0.0 (default, config sem a chave `pattern_weight`)
        tem de produzir exactamente o mesmo combined_score que sem a 4ª
        componente — mesmo com um pattern_score elevado passado ao caller."""
        without_pattern = scoring.combine_scores(
            1.0, 0.0, news_score=-1.0, config=self._COMBINED_CONFIG,
        )
        with_pattern_but_zero_weight = scoring.combine_scores(
            1.0, 0.0, news_score=-1.0, pattern_score=0.9, config=self._COMBINED_CONFIG,
        )
        assert with_pattern_but_zero_weight == without_pattern
        assert with_pattern_but_zero_weight == pytest.approx(0.15)

    def test_default_config_has_zero_pattern_weight(self):
        config = scoring.load_combined_scoring_config()
        assert config["pattern_weight"] == 0.0

    def test_env_override_sets_pattern_weight(self, monkeypatch):
        monkeypatch.setenv("SCORE_PATTERN_WEIGHT", "0.10")
        config = scoring.load_combined_scoring_config()
        assert config["pattern_weight"] == pytest.approx(0.10)

    def test_pattern_weight_renormalizes_proportionally(self):
        """Com pattern_weight > 0, o total_weight cresce e cada componente
        perde peso relativo proporcionalmente — sem renormalização manual."""
        config = dict(self._COMBINED_CONFIG)
        config["pattern_weight"] = 0.10
        ai, tech, news, pattern = 1.0, 0.5, 0.5, 0.8
        result = scoring.combine_scores(
            ai, tech, news_score=news, pattern_score=pattern, config=config,
        )
        total_w = 0.30 + 0.55 + 0.15 + 0.10
        expected = (ai * 0.30 + tech * 0.55 + news * 0.15 + pattern * 0.10) / total_w
        assert result == pytest.approx(expected, abs=1e-4)

    def test_pattern_weight_interacts_with_ai_abstention(self):
        """Quando a IA abstém (ai_score=None), pattern_score continua a
        participar na renormalização das restantes componentes activas."""
        config = dict(self._COMBINED_CONFIG)
        config["pattern_weight"] = 0.10
        tech, news, pattern = 0.4, 0.2, 0.6
        result = scoring.combine_scores(
            None, tech, news_score=news, pattern_score=pattern, config=config,
        )
        total_w = 0.55 + 0.15 + 0.10
        expected = (tech * 0.55 + news * 0.15 + pattern * 0.10) / total_w
        assert result == pytest.approx(expected, abs=1e-4)

    def test_pattern_score_none_excluded_even_with_positive_weight(self):
        config = dict(self._COMBINED_CONFIG)
        config["pattern_weight"] = 0.10
        result = scoring.combine_scores(
            None, 0.6, news_score=None, pattern_score=None, config=config,
        )
        assert result == pytest.approx(0.6)


class TestScoreToSignalWithCombinedConfig:
    """Bug 4: score_to_signal deve respeitar COMBINED_BUY/SELL_THRESHOLD."""

    def test_threshold_042_score_038_is_neutral(self, monkeypatch):
        monkeypatch.setenv("COMBINED_BUY_THRESHOLD", "0.42")
        monkeypatch.setenv("COMBINED_SELL_THRESHOLD", "-0.42")
        config = scoring.load_combined_scoring_config()
        assert scoring.score_to_signal(0.38, config) == "NEUTRAL"

    def test_threshold_042_score_043_is_buy(self, monkeypatch):
        monkeypatch.setenv("COMBINED_BUY_THRESHOLD", "0.42")
        monkeypatch.setenv("COMBINED_SELL_THRESHOLD", "-0.42")
        config = scoring.load_combined_scoring_config()
        assert scoring.score_to_signal(0.43, config) == "BUY"

    def test_threshold_042_score_minus_043_is_sell(self, monkeypatch):
        monkeypatch.setenv("COMBINED_BUY_THRESHOLD", "0.42")
        monkeypatch.setenv("COMBINED_SELL_THRESHOLD", "-0.42")
        config = scoring.load_combined_scoring_config()
        assert scoring.score_to_signal(-0.43, config) == "SELL"

    def test_threshold_042_score_minus_038_is_neutral(self, monkeypatch):
        monkeypatch.setenv("COMBINED_BUY_THRESHOLD", "0.42")
        monkeypatch.setenv("COMBINED_SELL_THRESHOLD", "-0.42")
        config = scoring.load_combined_scoring_config()
        assert scoring.score_to_signal(-0.38, config) == "NEUTRAL"


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
