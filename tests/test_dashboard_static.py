"""Smoke checks for dashboard AI analysis rendering hooks."""

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_dashboard_modal_renders_ai_analysis_text_with_fallback():
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    assert "Análise da IA" in html
    assert "d.ai_analysis_text || d.ai_reason" in html
    assert "Análise IA não disponível para esta decisão." in html
    assert "ai-analysis-text" in html
