"""Testes para normalização do JSON público do dashboard."""

import json

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


class TestNormaliseAiConfidence:
    """Regressão: `d.ai_confidence` no dashboard (web/index.html) lê o campo
    de topo — antes deste fix, `_normalise` só produzia `ai_confidence_score`
    (unit [0,1]), deixando `ai_confidence` sempre ausente/None no export."""

    def test_reconstructs_raw_confidence_from_unit_score(self):
        item = export_logs._normalise({
            "timestamp": "2026-05-06T19:00:00+00:00",
            "ai_confidence_score": 0.72,
        })
        assert item["ai_confidence"] == 72
        assert item["ai_confidence_score"] == 0.72

    def test_none_when_confidence_score_missing(self):
        item = export_logs._normalise({"timestamp": "2026-05-06T19:00:00+00:00"})
        assert item["ai_confidence"] is None

    def test_matches_features_snapshot_ai_confidence_when_present(self):
        item = export_logs._normalise({
            "timestamp": "2026-05-06T19:00:00+00:00",
            "ai_confidence_score": 0.05,
            "ai_features_snapshot": '{"ai_confidence": 5}',
        })
        assert item["ai_confidence"] == item["ai_features_snapshot"]["ai_confidence"] == 5


class TestNormaliseAdaptiveRisk:
    def test_surfaces_adaptive_fields_from_gate_diagnostics(self):
        item = export_logs._normalise({
            "timestamp": "2026-05-21T09:00:00+00:00",
            "gate_diagnostics_json": json.dumps({
                "adaptive_risk": {
                    "allow_trade": True,
                    "adaptive_min_confidence": 0.41,
                    "effective_confidence": 0.58,
                    "raw_confidence": 0.36,
                    "score_strength": 0.42,
                    "risk_multiplier": 0.5,
                    "dynamic_exposure": "small",
                    "execution_reason": "adaptive_risk_allowed: confidence=0.58 >= threshold=0.41",
                    "block_reason": None,
                    "bonuses": [{"name": "mtf_alignment", "value": 0.05}],
                    "penalties": [],
                    "context_blocks": [],
                }
            }),
        })
        adaptive = item["adaptive_risk"]
        assert adaptive["allow_trade"] is True
        assert adaptive["risk_multiplier"] == 0.5
        assert adaptive["effective_confidence"] == 0.58
        assert adaptive["adaptive_min_confidence"] == 0.41
        assert adaptive["dynamic_exposure"] == "small"
        assert adaptive["execution_reason"].startswith("adaptive_risk_allowed")
        assert adaptive["bonuses"][0]["name"] == "mtf_alignment"

    def test_empty_adaptive_when_diagnostics_absent(self):
        item = export_logs._normalise({"timestamp": "2026-05-21T09:00:00+00:00"})
        adaptive = item["adaptive_risk"]
        assert adaptive["risk_multiplier"] is None
        assert adaptive["effective_confidence"] is None
        assert adaptive["bonuses"] == []
        assert adaptive["penalties"] == []
        assert adaptive["context_blocks"] == []
