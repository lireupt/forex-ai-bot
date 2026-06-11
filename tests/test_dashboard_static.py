"""Smoke checks for dashboard AI analysis rendering hooks."""

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_dashboard_modal_renders_ai_analysis_text_with_fallback():
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    assert "Análise da IA" in html
    assert "d.ai_analysis_text || d.ai_reason" in html
    assert "Análise IA não disponível para esta decisão." in html
    assert "ai-analysis-text" in html


def test_dashboard_renders_macro_fields_in_table_and_modal():
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    assert "<th>Macro Risk</th>" in html
    assert "<th>Macro Event</th>" in html
    assert "<th>Macro Distance</th>" in html
    assert "<th>Macro Reason</th>" in html
    assert "<h4>Risco Macro</h4>" in html
    assert 'd.macro_event_time ? fmtTime(d.macro_event_time)' in html
