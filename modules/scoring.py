"""Conversão de sinais BUY/SELL/NEUTRAL para scores numéricos contínuos.

A ideia é manter os labels existentes (BUY/SELL/NEUTRAL) mas também guardar um
score em [-1.0, +1.0] para cada componente (IA, técnica, shadow, combinado),
permitindo análise mais fina sem alterar o gating real do bot.

Limiares e pesos são configuráveis por env vars. Os nomes novos são usados no
pipeline principal e os antigos SCORE_* continuam aceites por compatibilidade:
    COMBINED_BUY_THRESHOLD    (default  0.35)
    COMBINED_SELL_THRESHOLD   (default -0.35)
    AI_WEIGHT                 (default  0.30)
    TECHNICAL_WEIGHT          (default  0.55)
    NEWS_WEIGHT               (default  0.15)
    SCORE_SHADOW_WEIGHT       (default  0.0)  # compat/shadow opcional
"""

import os


DEFAULT_BUY_THRESHOLD = 0.35
DEFAULT_SELL_THRESHOLD = -0.35
DEFAULT_AI_WEIGHT = 0.6
DEFAULT_TECHNICAL_WEIGHT = 0.4
DEFAULT_COMBINED_BUY_THRESHOLD = 0.35
DEFAULT_COMBINED_SELL_THRESHOLD = -0.35
DEFAULT_COMBINED_AI_WEIGHT = 0.30
DEFAULT_COMBINED_TECHNICAL_WEIGHT = 0.55
DEFAULT_NEWS_WEIGHT = 0.15
DEFAULT_SHADOW_WEIGHT = 0.0


def _env_float(name, default):
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _env_float_any(names, default):
    for name in names:
        value = os.getenv(name)
        if value is None or value.strip() == "":
            continue
        try:
            return float(value)
        except ValueError:
            return default
    return default


def load_scoring_config():
    """DEPRECATED: config legada (AI=0.6/tech=0.4, news=0.0, threshold=0.35).

    Usada apenas por testes legados e ferramentas antigas. NÃO usar no pipeline
    principal — usar `load_combined_scoring_config()` que lê as env vars correctas
    (AI_WEIGHT, TECHNICAL_WEIGHT, NEWS_WEIGHT, COMBINED_BUY/SELL_THRESHOLD).
    """
    return {
        "buy_threshold": _env_float("SCORE_BUY_THRESHOLD", DEFAULT_BUY_THRESHOLD),
        "sell_threshold": _env_float("SCORE_SELL_THRESHOLD", DEFAULT_SELL_THRESHOLD),
        "ai_weight": _env_float("SCORE_AI_WEIGHT", DEFAULT_AI_WEIGHT),
        "technical_weight": _env_float("SCORE_TECHNICAL_WEIGHT", DEFAULT_TECHNICAL_WEIGHT),
        "news_weight": 0.0,
        "shadow_weight": _env_float("SCORE_SHADOW_WEIGHT", DEFAULT_SHADOW_WEIGHT),
    }


def load_combined_scoring_config():
    """Configuração do agregador principal novo.

    Mantém `load_scoring_config()` estável para ferramentas/testes antigos e
    usa os nomes explícitos do pipeline combinado para a estratégia principal.
    """
    return {
        "buy_threshold": _env_float("COMBINED_BUY_THRESHOLD", DEFAULT_COMBINED_BUY_THRESHOLD),
        "sell_threshold": _env_float("COMBINED_SELL_THRESHOLD", DEFAULT_COMBINED_SELL_THRESHOLD),
        "ai_weight": _env_float("AI_WEIGHT", DEFAULT_COMBINED_AI_WEIGHT),
        "technical_weight": _env_float("TECHNICAL_WEIGHT", DEFAULT_COMBINED_TECHNICAL_WEIGHT),
        "news_weight": _env_float("NEWS_WEIGHT", DEFAULT_NEWS_WEIGHT),
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


def technical_votes_score(rsi_vote, ema_vote, macd_vote, weights=None):
    """Score técnico contínuo a partir dos 3 votos.

    Cada voto: bullish=+1, bearish=-1, neutral=0. Soma dividida por 3 dá um
    score natural em [-1, 1]. EMA20>EMA50 com MACD>signal e RSI normal já dá
    cerca de 0.66 (favorece BUY) sem precisar de RSI extremo.
    """
    mapping = {"bullish": 1.0, "bearish": -1.0}
    weights = weights or {"rsi": 1 / 3, "ema": 1 / 3, "macd": 1 / 3}
    rsi_w = float(weights.get("rsi", 0.0) or 0.0)
    ema_w = float(weights.get("ema", 0.0) or 0.0)
    macd_w = float(weights.get("macd", 0.0) or 0.0)
    total_weight = abs(rsi_w) + abs(ema_w) + abs(macd_w)
    if total_weight == 0:
        return 0.0
    total = (
        mapping.get(rsi_vote, 0.0) * rsi_w
        + mapping.get(ema_vote, 0.0) * ema_w
        + mapping.get(macd_vote, 0.0) * macd_w
    )
    return _clamp(total / total_weight)


def score_to_signal(score, config=None):
    if score is None:
        return "NEUTRAL"
    config = config or load_scoring_config()
    if score >= config["buy_threshold"]:
        return "BUY"
    if score <= config["sell_threshold"]:
        return "SELL"
    return "NEUTRAL"


def combine_scores(ai_score, technical_score, shadow_score=None, news_score=None, config=None):
    """Combina scores num único valor ponderado em [-1, 1].

    Componentes com valor None são excluídos do numerador E do denominador
    (renormalização proporcional). Componentes com valor 0.0 passados como None
    pelo caller também são excluídos — é semanticamente correcto: 0.0 = neutral,
    sem informação nova, não deve dilatar o denominador.

    O default usa `load_combined_scoring_config()` (pesos do pipeline principal:
    AI=0.30, tech=0.55, news=0.15, threshold=COMBINED_BUY/SELL_THRESHOLD).
    """
    config = config or load_combined_scoring_config()
    ai_w = config["ai_weight"]
    tech_w = config["technical_weight"]
    news_w = config.get("news_weight", 0.0)
    shadow_w = config["shadow_weight"]

    weighted_sum = 0.0
    total_weight = 0.0
    if ai_score is not None and ai_w > 0:
        weighted_sum += ai_score * ai_w
        total_weight += ai_w
    if technical_score is not None and tech_w > 0:
        weighted_sum += technical_score * tech_w
        total_weight += tech_w
    if news_score is not None and news_w > 0:
        weighted_sum += news_score * news_w
        total_weight += news_w
    if shadow_score is not None and shadow_w > 0:
        weighted_sum += shadow_score * shadow_w
        total_weight += shadow_w

    if total_weight == 0:
        return 0.0
    return _clamp(round(weighted_sum / total_weight, 4))


_SENTIMENT_BASE = {"positive": 0.5, "negative": -0.5, "mixed": 0.0, "neutral": 0.0}
_BIAS_DIRECTION = {"BUY": 1.0, "SELL": -1.0, "NEUTRAL": 0.0}


def news_score(ai_result, news_items):
    """Score de notícias fundamentado em [-1, 1], calculado a partir dos outputs
    já normalizados da IA (bias, news_sentiment, confidence_adjustment).

    Inspirado no padrão "Grounded Sentiment Analyst" (TradingAgents, arXiv:2412.20138):
    anchoring em dados concretos, sem nova chamada LLM, totalmente auditável.

    Devolve (score, basis):
    - score: float em [-1, 1], ou 0.0 se não houver notícias ou IA vazia
    - basis: string de auditoria com os inputs usados no cálculo

    score = base(sentiment) + direction(bias) × |confidence_adjustment|
    Neutro/mixed → base=0.0; clampado a [-1.0, 1.0].
    0.0 ≡ sem informação → caller deve passar `news_score or None` ao combine_scores.
    """
    if not ai_result or not news_items:
        return 0.0, "no_news"

    sentiment = (ai_result.get("news_sentiment") or "neutral").lower()
    bias = (ai_result.get("bias") or "NEUTRAL").upper()
    try:
        confidence_adjustment = float(ai_result.get("confidence_adjustment") or 0.0)
    except (TypeError, ValueError):
        confidence_adjustment = 0.0

    base = _SENTIMENT_BASE.get(sentiment, 0.0)
    direction = _BIAS_DIRECTION.get(bias, 0.0)
    score = base + direction * abs(confidence_adjustment)
    score = round(max(-1.0, min(1.0, score)), 4)

    basis = (
        f"sentiment={sentiment} bias={bias} "
        f"adj={confidence_adjustment:+.4f} n_articles={len(news_items)}"
    )
    return score, basis


def confidence_to_unit(confidence):
    """Converte confidence 0..100 para 0..1 (ai_confidence_score)."""
    if confidence is None:
        return 0.0
    try:
        value = float(confidence)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(100.0, value)) / 100.0
