"""Testes para normalização do JSON público do dashboard."""

from scripts import export_logs


class TestNormaliseAIObservability:
    def test_prefers_ai_observability_fields(self):
        item = export_logs._normalise({
            "timestamp": "2026-05-06T19:00:00+00:00",
            "ai_analysis_text": "Texto completo da IA.",
            "ai_reason": "Razão principal.",
            "ai_model_version": "groq:model",
            "ai_features_snapshot": '{"close": 1.1756}',
        })
        assert item["ai_analysis_text"] == "Texto completo da IA."
        assert item["ai_reason"] == "Razão principal."
        assert item["ai_model_version"] == "groq:model"
        assert item["ai_features_snapshot"]["close"] == 1.1756

    def test_accepts_legacy_reasoning_field(self):
        item = export_logs._normalise({
            "timestamp": "2026-05-06T19:00:00+00:00",
            "reasoning": "Campo antigo com explicação. Segunda frase. Terceira frase.",
            "provider": "groq",
        })
        assert item["ai_analysis_text"] == "Campo antigo com explicação. Segunda frase. Terceira frase."
        assert item["ai_reason"] == "Campo antigo com explicação. Segunda frase."
        assert item["ai_model_version"] == "groq"
        assert item["ai_features_snapshot"] == {}

    def test_fallback_text_when_analysis_missing(self):
        item = export_logs._normalise({
            "timestamp": "2026-05-06T19:00:00+00:00",
        })
        assert item["ai_analysis_text"] == export_logs.AI_ANALYSIS_UNAVAILABLE
