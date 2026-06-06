"""Testes para `modules.ai_aggregator` — prompt, validação e fallback.

Não chamamos APIs externas: testamos helpers puros, a normalização da resposta
e o fallback seguro quando o provider falha ou não está configurado.
"""

import json

import pytest

from modules import ai_aggregator


SAMPLE_SNAPSHOT = {
    "pair": "EUR/USD",
    "technical": {
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
        "technical_signal": "BUY",
        "technical_score": 0.42,
        "multi_timeframe_score": 0.38,
        "timeframe_alignment": "h1_h4_aligned",
    },
    "fundamental": {
        "ai_bias": "NEUTRAL",
        "ai_confidence": 20,
        "macro_context": "mixed",
        "news_sentiment": "neutral",
        "dangerous_event_nearby": False,
    },
    "performance": {"winrate": 50.0, "loss_streak": 1, "net_pips": 12.0},
    "operational_risk": {"market_open": True, "can_open_trade": True, "cooldown_active": False},
    "filters": {"dry_run": True, "allow_buy": True, "allow_sell": True, "trade_allowed": True},
    "preliminary_recommendation": {"combined_signal": "BUY", "gating_signal": "BUY", "hold_off": False},
}


class TestBuildUserMessage:
    def test_includes_all_layers(self):
        text = ai_aggregator.build_aggregation_input(SAMPLE_SNAPSHOT)
        assert "CAMADA 1 — TÉCNICO" in text
        assert "CAMADA 2 — FUNDAMENTAL/EVENTOS" in text
        assert "CAMADA 3 — PERFORMANCE/CONTEXTO RECENTE" in text
        assert "RISCO OPERACIONAL" in text
        assert "ESTADO DOS FILTROS/LIMITAÇÕES" in text
        assert "RECOMENDAÇÃO TÉCNICA PRELIMINAR" in text

    def test_includes_key_values(self):
        text = ai_aggregator.build_aggregation_input(SAMPLE_SNAPSHOT)
        assert "EUR/USD" in text
        assert "58.2" in text
        assert "h1_h4_aligned" in text

    def test_handles_empty_snapshot(self):
        text = ai_aggregator.build_aggregation_input({})
        assert "CAMADA 1 — TÉCNICO" in text


class TestAggregatedScore:
    def test_buy_positive(self):
        assert ai_aggregator.aggregated_score("BUY", 80) == pytest.approx(0.80)

    def test_sell_negative(self):
        assert ai_aggregator.aggregated_score("SELL", 60) == pytest.approx(-0.60)

    def test_neutral_zero(self):
        assert ai_aggregator.aggregated_score("NEUTRAL", 90) == 0.0

    def test_confidence_clamped(self):
        assert ai_aggregator.aggregated_score("BUY", 250) == pytest.approx(1.0)


class TestValidate:
    def _valid(self, **overrides):
        base = {
            "ai_aggregated_signal": "buy",
            "ai_aggregated_confidence": 72,
            "reasoning_summary": "Técnica e MTF alinhadas.",
            "risk_level": "medium",
            "supporting_factors": ["mtf alinhado", "rsi ok"],
            "contradicting_factors": ["macd bearish"],
            "should_trade": True,
            "should_reduce_risk": False,
            "warnings": [],
        }
        base.update(overrides)
        return base

    def test_normalises_signal_and_score(self):
        result = ai_aggregator._validate(self._valid())
        assert result["ai_aggregated_signal"] == "BUY"
        assert result["ai_aggregated_score"] == pytest.approx(0.72)

    def test_clamps_confidence(self):
        result = ai_aggregator._validate(self._valid(ai_aggregated_confidence=300))
        assert result["ai_aggregated_confidence"] == 100

    def test_invalid_signal_becomes_neutral(self):
        result = ai_aggregator._validate(self._valid(ai_aggregated_signal="LONG"))
        assert result["ai_aggregated_signal"] == "NEUTRAL"

    def test_invalid_risk_level_becomes_medium(self):
        result = ai_aggregator._validate(self._valid(risk_level="extreme"))
        assert result["risk_level"] == "medium"

    def test_coerces_string_factor_to_list(self):
        result = ai_aggregator._validate(self._valid(supporting_factors="apenas um"))
        assert result["supporting_factors"] == ["apenas um"]

    def test_raises_on_missing_field(self):
        bad = self._valid()
        del bad["should_trade"]
        with pytest.raises(ValueError):
            ai_aggregator._validate(bad)


class TestAnalyseFallback:
    def test_invalid_provider(self, monkeypatch):
        monkeypatch.setenv("AI_AGGREGATOR_PROVIDER", "nope")
        result = ai_aggregator.analyse(SAMPLE_SNAPSHOT)
        assert result["status"] == "failed"
        assert result["ai_aggregated_signal"] == "NEUTRAL"
        assert result["should_trade"] is False
        assert result["should_reduce_risk"] is True

    def test_groq_key_missing(self, monkeypatch):
        monkeypatch.setenv("AI_AGGREGATOR_PROVIDER", "groq")
        result = ai_aggregator.analyse(SAMPLE_SNAPSHOT)
        assert result["status"] == "failed"
        assert "GROQ_API_KEY" in result["error"]

    def test_falls_back_to_ai_provider_env(self, monkeypatch):
        monkeypatch.setenv("AI_PROVIDER", "claude")
        result = ai_aggregator.analyse(SAMPLE_SNAPSHOT)
        assert result["provider"] == "claude"


class TestAnalyseSuccess:
    def test_parses_mocked_llm_response(self, monkeypatch):
        monkeypatch.setenv("AI_AGGREGATOR_PROVIDER", "groq")
        payload = {
            "ai_aggregated_signal": "BUY",
            "ai_aggregated_confidence": 68,
            "reasoning_summary": "Confluência técnica.",
            "risk_level": "low",
            "supporting_factors": ["mtf", "rsi"],
            "contradicting_factors": [],
            "should_trade": True,
            "should_reduce_risk": False,
            "warnings": [],
        }
        monkeypatch.setattr(ai_aggregator, "_analyse_groq", lambda msg: json.dumps(payload))
        result = ai_aggregator.analyse(SAMPLE_SNAPSHOT)
        assert result["status"] == "ok"
        assert result["ai_aggregated_signal"] == "BUY"
        assert result["ai_aggregated_score"] == pytest.approx(0.68)
        assert result["provider"] == "groq"
        assert result["model_version"].startswith("groq:")

    def test_handles_json_fences(self, monkeypatch):
        monkeypatch.setenv("AI_AGGREGATOR_PROVIDER", "groq")
        payload = {
            "ai_aggregated_signal": "SELL",
            "ai_aggregated_confidence": 55,
            "reasoning_summary": "x",
            "risk_level": "high",
            "supporting_factors": [],
            "contradicting_factors": [],
            "should_trade": False,
            "should_reduce_risk": True,
            "warnings": ["evento próximo"],
        }
        fenced = "```json\n" + json.dumps(payload) + "\n```"
        monkeypatch.setattr(ai_aggregator, "_analyse_groq", lambda msg: fenced)
        result = ai_aggregator.analyse(SAMPLE_SNAPSHOT)
        assert result["status"] == "ok"
        assert result["ai_aggregated_signal"] == "SELL"
