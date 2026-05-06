"""Testes para normalização do JSON público do dashboard."""

from scripts import export_logs


class TestNormaliseAIObservability:
    def test_prefers_ai_observability_fields(self):
        item = export_logs._normalise({
            "timestamp": "2026-05-06T19:00:00+00:00",
            "ai_reason": "Razão principal.",
            "ai_model_version": "groq:model",
            "ai_features_snapshot": '{"close": 1.1756}',
        })
        assert item["ai_reason"] == "Razão principal."
        assert item["ai_model_version"] == "groq:model"
        assert item["ai_features_snapshot"]["close"] == 1.1756

    def test_accepts_legacy_reasoning_field(self):
        item = export_logs._normalise({
            "timestamp": "2026-05-06T19:00:00+00:00",
            "reasoning": "Campo antigo com explicação.",
            "provider": "groq",
        })
        assert item["ai_reason"] == "Campo antigo com explicação."
        assert item["ai_model_version"] == "groq"
        assert item["ai_features_snapshot"] == {}
