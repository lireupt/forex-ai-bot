"""Testes para `modules.ai_analyst` — formatação do prompt e fallback.

Não chamamos APIs externas; testamos apenas os helpers puros e o fallback
quando o provider não está configurado.
"""

import pytest

from modules import ai_analyst


SAMPLE_TECHNICAL = {
    "signal": "NEUTRAL",
    "confidence": 33,
    "technical_reason": "RSI neutral; EMA bullish; MACD bearish.",
    "indicators": {
        "current_price": 1.1756,
        "rsi": 58.2,
        "rsi_signal": "neutral",
        "ema20": 1.1747,
        "ema50": 1.1732,
        "ema_trend": "bullish",
        "macd": 0.0011,
        "macd_signal_value": 0.0013,
        "macd_signal": "bearish",
        "atr_pips": 12.6,
        "volatility_reason": "Volatilidade normal: ATR14 em 12.6 pips.",
    },
}


class TestFormatTechnical:
    def test_returns_none_when_empty(self):
        assert ai_analyst._format_technical(None) is None
        assert ai_analyst._format_technical({}) is None
        assert ai_analyst._format_technical({"indicators": {}}) is None

    def test_includes_indicators(self):
        text = ai_analyst._format_technical(SAMPLE_TECHNICAL)
        assert "RSI(14)" in text
        assert "58.2" in text
        assert "EMA20 vs EMA50" in text
        assert "1.1747" in text
        assert "MACD" in text
        assert "ATR(14)" in text
        assert "12.6 pips" in text

    def test_includes_signal_summary(self):
        text = ai_analyst._format_technical(SAMPLE_TECHNICAL)
        assert "Resumo técnico estrito: NEUTRAL" in text
        assert "33%" in text
        assert "RSI neutral" in text


class TestBuildAnalysisInput:
    def test_without_technical(self):
        text = ai_analyst.build_analysis_input(
            news=[{"title": "Fed cuts rates", "source": "Reuters"}],
            events=[],
            pair="EUR/USD",
        )
        assert "Par: EUR/USD" in text
        assert "Fed cuts rates" in text
        assert "SNAPSHOT TÉCNICO" not in text

    def test_with_technical(self):
        text = ai_analyst.build_analysis_input(
            news=[{"title": "Fed cuts rates", "source": "Reuters"}],
            events=[],
            pair="EUR/USD",
            technical=SAMPLE_TECHNICAL,
        )
        assert "SNAPSHOT TÉCNICO ACTUAL" in text
        assert "RSI(14)" in text
        assert "Fed cuts rates" in text

    def test_input_hash_changes_with_technical(self):
        """Garantia: adicionar snapshot técnico muda o input → invalida cache.
        Isto é desejado: quando o gráfico muda, queremos a IA a re-analisar."""
        without = ai_analyst.build_analysis_input([], [], "EUR/USD")
        with_tech = ai_analyst.build_analysis_input([], [], "EUR/USD", technical=SAMPLE_TECHNICAL)
        assert without != with_tech


class TestAnalyseFallback:
    def test_fallback_when_provider_invalid(self, monkeypatch):
        monkeypatch.setenv("AI_PROVIDER", "not_a_provider")
        result = ai_analyst.analyse([], [], "EUR/USD")
        assert result["signal"] == "NEUTRAL"
        assert result["confidence"] == 0
        assert result["hold_off"] is True
        assert result["risk_level"] == "HIGH"
        assert "inválido" in result["reasoning"]

    def test_fallback_when_groq_key_missing(self, monkeypatch):
        monkeypatch.setenv("AI_PROVIDER", "groq")
        # Sem GROQ_API_KEY (já limpo pelo conftest)
        result = ai_analyst.analyse([], [], "EUR/USD")
        assert result["signal"] == "NEUTRAL"
        assert "GROQ_API_KEY" in result["reasoning"]

    def test_fallback_when_claude_key_missing(self, monkeypatch):
        monkeypatch.setenv("AI_PROVIDER", "claude")
        result = ai_analyst.analyse([], [], "EUR/USD")
        assert result["signal"] == "NEUTRAL"
        assert "ANTHROPIC_API_KEY" in result["reasoning"]

    def test_fallback_records_provider(self, monkeypatch):
        monkeypatch.setenv("AI_PROVIDER", "groq")
        result = ai_analyst.analyse([], [], "EUR/USD")
        assert result["provider"] == "groq"


class TestModelVersion:
    def test_groq_returns_groq_model(self):
        v = ai_analyst.model_version_for_provider("groq")
        assert v.startswith("groq:")
        assert ai_analyst.GROQ_MODEL in v

    def test_claude_returns_claude_model(self):
        v = ai_analyst.model_version_for_provider("claude")
        assert v.startswith("claude:")
        assert ai_analyst.CLAUDE_MODEL in v

    def test_unknown_provider_unknown(self):
        assert ai_analyst.model_version_for_provider("") == "unknown"
        assert ai_analyst.model_version_for_provider(None) == "unknown"
        assert ai_analyst.model_version_for_provider("foo") == "foo"
