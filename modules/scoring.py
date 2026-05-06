"""Conversão de sinais BUY/SELL/NEUTRAL para scores numéricos contínuos.

A ideia é manter os labels existentes (BUY/SELL/NEUTRAL) mas também guardar um
score em [-1.0, +1.0] para cada componente (IA, técnica, shadow, combinado),
permitindo análise mais fina sem alterar o gating real do bot.

Limiares e pesos são configuráveis por env vars:
    SCORE_BUY_THRESHOLD       (default  0.35)
    SCORE_SELL_THRESHOLD      (default -0.35)
    SCORE_AI_WEIGHT           (default  0.6)
    SCORE_TECHNICAL_WEIGHT    (default  0.4)
    SCORE_SHADOW_WEIGHT       (default  0.0)  # se > 0, entra na média ponderada
"""

import os


DEFAULT_BUY_THRESHOLD = 0.35
DEFAULT_SELL_THRESHOLD = -0.35
DEFAULT_AI_WEIGHT = 0.6
DEFAULT_TECHNICAL_WEIGHT = 0.4
DEFAULT_SHADOW_WEIGHT = 0.0


def _env_float(name, default):
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    try:
        return float(value)
    except ValueError:
        return default


def load_scoring_config():
    return {
        "buy_threshold": _env_float("SCORE_BUY_THRESHOLD", DEFAULT_BUY_THRESHOLD),
        "sell_threshold": _env_float("SCORE_SELL_THRESHOLD", DEFAULT_SELL_THRESHOLD),
        "ai_weight": _env_float("SCORE_AI_WEIGHT", DEFAULT_AI_WEIGHT),
        "technical_weight": _env_float("SCORE_TECHNICAL_WEIGHT", DEFAULT_TECHNICAL_WEIGHT),
        "shadow_weight": _env_float("SCORE_SHADOW_WEIGHT", DEFAULT_SHADOW_WEIGHT),
    }


def _direction(signal):
    if signal == "BUY":
        return 1.0
    if signal == "SELL":
        return -1.0
    return 0.0


def _clamp(value, lo=-1.0, hi=1.0):
    if value is None:
        return None
    return max(lo, min(hi, value))


def signal_score(signal, confidence):
    """Converte (signal, confidence%) em score em [-1, 1].

    BUY 80% -> +0.80, SELL 60% -> -0.60, NEUTRAL -> 0.0
    """
    direction = _direction(signal)
    if direction == 0.0:
        return 0.0
    try:
        conf = float(confidence or 0)
    except (TypeError, ValueError):
        conf = 0.0
    conf = max(0.0, min(100.0, conf)) / 100.0
    return _clamp(direction * conf)


def technical_votes_score(rsi_vote, ema_vote, macd_vote):
    """Score técnico contínuo a partir dos 3 votos.

    Cada voto: bullish=+1, bearish=-1, neutral=0. Soma dividida por 3 dá um
    score natural em [-1, 1]. EMA20>EMA50 com MACD>signal e RSI normal já dá
    cerca de 0.66 (favorece BUY) sem precisar de RSI extremo.
    """
    mapping = {"bullish": 1.0, "bearish": -1.0}
    total = (
        mapping.get(rsi_vote, 0.0)
        + mapping.get(ema_vote, 0.0)
        + mapping.get(macd_vote, 0.0)
    )
    return _clamp(total / 3.0)


def score_to_signal(score, config=None):
    if score is None:
        return "NEUTRAL"
    config = config or load_scoring_config()
    if score >= config["buy_threshold"]:
        return "BUY"
    if score <= config["sell_threshold"]:
        return "SELL"
    return "NEUTRAL"


def combine_scores(ai_score, technical_score, shadow_score=None, config=None):
    """Combina scores num único valor ponderado em [-1, 1]."""
    config = config or load_scoring_config()
    ai_w = config["ai_weight"]
    tech_w = config["technical_weight"]
    shadow_w = config["shadow_weight"]

    weighted_sum = 0.0
    total_weight = 0.0
    if ai_score is not None and ai_w > 0:
        weighted_sum += ai_score * ai_w
        total_weight += ai_w
    if technical_score is not None and tech_w > 0:
        weighted_sum += technical_score * tech_w
        total_weight += tech_w
    if shadow_score is not None and shadow_w > 0:
        weighted_sum += shadow_score * shadow_w
        total_weight += shadow_w

    if total_weight == 0:
        return 0.0
    return _clamp(round(weighted_sum / total_weight, 4))


def confidence_to_unit(confidence):
    """Converte confidence 0..100 para 0..1 (ai_confidence_score)."""
    if confidence is None:
        return 0.0
    try:
        value = float(confidence)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(100.0, value)) / 100.0
