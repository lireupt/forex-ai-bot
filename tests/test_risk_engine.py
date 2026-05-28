import pytest

from modules.risk import evaluate_trade
from modules.risk_engine import AdaptiveRiskEngine


def _config(**overrides):
    base = {
        "adaptive_base_min_confidence": 0.45,
        "adaptive_min_floor": 0.35,
        "adaptive_min_ceiling": 0.65,
        "max_spread_pips": 2.5,
        "atr_extreme_pips": 35.0,
        "min_atr_pips": 8.5,
        "block_extreme_news_risk": False,
    }
    base.update(overrides)
    return base


def test_score_around_048_is_moderate_with_persistence_and_mtf():
    engine = AdaptiveRiskEngine(_config())
    decision = engine.evaluate(
        signal="SELL",
        confidence=48,
        combined_signal={"signal": "SELL", "combined_score": -0.48},
        atr_pips=14.0,
        gate_context={
            "combined_score": -0.48,
            "ai_signal": "SELL",
            "ai_confidence_score": 0.72,
            "mtf_signal": "SELL",
            "timeframe_alignment": "h1_h4_aligned",
            "signal_persistence": 3,
            "market": {"is_open": True},
            "operational": {"can_open_trade": True},
            "cooldown": {},
        },
    )

    assert decision["allow_trade"] is True
    assert decision["adaptive_min_confidence"] < 0.45
    assert decision["effective_confidence"] >= 0.55
    assert decision["risk_multiplier"] in (0.75, 1.0)


def test_spread_above_max_blocks_even_with_good_confidence():
    engine = AdaptiveRiskEngine(_config(max_spread_pips=2.0))
    decision = engine.evaluate(
        signal="BUY",
        confidence=70,
        combined_signal={"signal": "BUY", "combined_score": 0.70},
        gate_context={
            "combined_score": 0.70,
            "spread_pips": 2.6,
            "market": {"is_open": True},
            "operational": {"can_open_trade": True},
            "cooldown": {},
        },
    )

    assert decision["allow_trade"] is False
    assert "spread_above_max" in decision["context_blocks"]


def test_evaluate_trade_uses_adaptive_gate_instead_of_fixed_65(monkeypatch):
    monkeypatch.setenv("ALLOW_SELL", "true")
    monkeypatch.setenv("ATR_FILTER_ENABLED", "false")
    monkeypatch.setenv("MOMENTUM_FILTER_ENABLED", "false")
    result = evaluate_trade(
        "EUR/USD",
        {
            "signal": "SELL",
            "confidence": 48,
            "hold_off": False,
            "combined_score": -0.48,
            "reasoning": "score SELL moderado persistente",
        },
        1.1700,
        event_risk={"dangerous_event_nearby": False, "dangerous_event_reason": ""},
        atr_pips=14.0,
        gate_context={
            "combined_score": -0.48,
            "ai_signal": "SELL",
            "ai_confidence_score": 0.72,
            "mtf_signal": "SELL",
            "timeframe_alignment": "h1_h4_aligned",
            "signal_persistence": 3,
            "market": {"is_open": True},
            "operational": {"can_open_trade": True},
            "cooldown": {},
        },
    )

    assert result["trade_allowed"] is True
    assert result["adaptive_risk"]["adaptive_min_confidence"] < 0.45
    assert result["simulated_order"]["risk_multiplier"] == pytest.approx(
        result["adaptive_risk"]["risk_multiplier"]
    )


def test_neutral_combined_stays_blocked_without_engine(monkeypatch):
    monkeypatch.setenv("ALLOW_SELL", "true")
    result = evaluate_trade(
        "EUR/USD",
        {
            "signal": "NEUTRAL",
            "confidence": 24,
            "hold_off": False,
            "combined_score": -0.24,
            "neutral_reason": "sinal combinado é NEUTRAL",
        },
        1.16140,
        event_risk={"dangerous_event_nearby": False, "dangerous_event_reason": ""},
        atr_pips=9.6,
        gate_context={
            "combined_score": -0.24,
            "market": {"is_open": True},
            "operational": {"can_open_trade": True},
            "cooldown": {},
        },
    )

    assert result["trade_allowed"] is False
    assert result["block_reason"] == "sinal combinado é NEUTRAL"
    assert result["simulated_order"] is None
    # NEUTRAL retorna antes do gate de execução: a engine nem corre.
    assert "adaptive_risk" not in result


def test_minimal_directional_signal_opens_with_reduced_size(monkeypatch):
    monkeypatch.setenv("ALLOW_SELL", "true")
    monkeypatch.setenv("MIN_CONFIDENCE_TO_TRADE", "0.45")
    result = evaluate_trade(
        "EUR/USD",
        {
            "signal": "SELL",
            "confidence": 36,
            "hold_off": False,
            "combined_score": -0.36,
            "reasoning": "score SELL no limiar direcional",
        },
        1.17000,
        event_risk={"dangerous_event_nearby": False, "dangerous_event_reason": ""},
        atr_pips=8.2,  # abaixo do min_atr default 8.5 -> nota soft, não bloqueia
        technical_indicators={},
        gate_context={
            "combined_score": -0.36,
            "market": {"is_open": True},
            "operational": {"can_open_trade": True},
            "cooldown": {},
        },
    )

    assert result["trade_allowed"] is True
    # Penalty soft de baixa volatilidade fica registada mas não bloqueia.
    assert "low_volatility_penalty_applied" in result["gate_reasons"]
    assert result["block_reason"] is None
    order = result["simulated_order"]
    assert 0.0 < order["risk_multiplier"] < 1.0
    assert order["risk_percent"] < order["base_risk_percent"]


def test_confidence_below_calibration_threshold_blocks(monkeypatch):
    monkeypatch.setenv("ALLOW_SELL", "true")
    monkeypatch.setenv("MIN_CONFIDENCE_TO_TRADE", "0.55")
    result = evaluate_trade(
        "EUR/USD",
        {
            "signal": "SELL",
            "confidence": 36,
            "hold_off": False,
            "combined_score": -0.36,
            "reasoning": "score SELL no limiar direcional",
        },
        1.17000,
        event_risk={"dangerous_event_nearby": False, "dangerous_event_reason": ""},
        atr_pips=14.0,
        gate_context={
            "combined_score": -0.36,
            "market": {"is_open": True},
            "operational": {"can_open_trade": True},
            "cooldown": {},
        },
    )

    assert result["trade_allowed"] is False
    assert result["block_reason"] == "confidence_below_threshold"
