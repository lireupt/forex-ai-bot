"""Agregação técnica multi-timeframe para EUR/USD.

Mantém H1 como referência principal, mas usa M15/H4/D1 para ajustar timing,
tendência intermédia e filtro macro sem exigir consenso total.
"""

from modules import scoring


TIMEFRAME_WEIGHTS = {
    "m15": 0.20,
    "h1": 0.40,
    "h4": 0.30,
    "d1": 0.10,
}

ORDERED_TIMEFRAMES = ("m15", "h1", "h4", "d1")


def _clamp(value):
    return max(-1.0, min(1.0, float(value or 0.0)))


def _tf_score(result):
    indicators = (result or {}).get("indicators") or {}
    score = indicators.get("technical_score")
    if score is None:
        score = 0.0
    return _clamp(score)


def _direction(score, threshold=0.35):
    if score >= threshold:
        return "BUY"
    if score <= -threshold:
        return "SELL"
    return "NEUTRAL"


def _opposes(left, right, threshold=0.35):
    return abs(left) >= threshold and abs(right) >= threshold and left * right < 0


def aggregate(results_by_timeframe, weights=None):
    """Combina resultados técnicos por timeframe.

    `results_by_timeframe` deve usar chaves canónicas: m15, h1, h4, d1.
    Timeframes ausentes contam como NEUTRAL 0.0, sem bloquear o restante.
    """
    weights = weights or TIMEFRAME_WEIGHTS
    scores = {tf: _tf_score(results_by_timeframe.get(tf)) for tf in ORDERED_TIMEFRAMES}

    total_weight = sum(float(weights.get(tf, 0.0) or 0.0) for tf in ORDERED_TIMEFRAMES)
    if total_weight <= 0:
        multi_score = 0.0
    else:
        multi_score = sum(scores[tf] * float(weights.get(tf, 0.0) or 0.0) for tf in ORDERED_TIMEFRAMES)
        multi_score = _clamp(multi_score / total_weight)

    h1 = scores["h1"]
    h4 = scores["h4"]
    d1 = scores["d1"]
    m15 = scores["m15"]
    h1_signal = _direction(h1)
    signal = scoring.score_to_signal(
        multi_score,
        {"buy_threshold": 0.35, "sell_threshold": -0.35},
    )

    alignment = "mixed"
    block_reason = ""
    confidence_adjustment = 1.0
    notes = []

    if h1_signal in ("BUY", "SELL") and _direction(h4) == h1_signal:
        alignment = "h1_h4_aligned"
        confidence_adjustment = 1.10
        notes.append("H1 e H4 alinhados")

    if h1_signal in ("BUY", "SELL") and _opposes(h1, h4) and _opposes(h1, d1):
        alignment = "h4_d1_strongly_against_h1"
        block_reason = "H4 e D1 fortemente contra H1"
        confidence_adjustment = 0.25
        notes.append(block_reason)
    elif h1_signal in ("BUY", "SELL") and _opposes(h1, h4):
        alignment = "h4_against_h1"
        confidence_adjustment = min(confidence_adjustment, 0.70)
        notes.append("H4 contra H1")
    elif h1_signal in ("BUY", "SELL") and _opposes(h1, d1):
        alignment = "d1_strongly_against_h1"
        confidence_adjustment = min(confidence_adjustment, 0.85)
        notes.append("D1 contra H1")

    if h1_signal in ("BUY", "SELL") and _opposes(h1, m15):
        if _direction(h4) == h1_signal:
            notes.append("M15 contra H1; entrada menos ideal")
            if alignment == "h1_h4_aligned":
                alignment = "h1_h4_aligned_m15_against"
            confidence_adjustment = min(confidence_adjustment, 0.90)
        else:
            notes.append("M15 contra H1")
            confidence_adjustment = min(confidence_adjustment, 0.80)

    if alignment == "mixed" and all(_direction(scores[tf]) == h1_signal and h1_signal != "NEUTRAL" for tf in ORDERED_TIMEFRAMES):
        alignment = f"all_aligned_{h1_signal.lower()}"
        confidence_adjustment = 1.15
        notes.append("Todos os timeframes alinhados")

    if alignment == "mixed" and h1_signal == "NEUTRAL":
        alignment = "h1_neutral"

    return {
        "technical_score_m15": round(scores["m15"], 4),
        "technical_score_h1": round(scores["h1"], 4),
        "technical_score_h4": round(scores["h4"], 4),
        "technical_score_d1": round(scores["d1"], 4),
        "multi_timeframe_score": round(multi_score, 4),
        "multi_timeframe_signal": signal,
        "timeframe_alignment": alignment,
        "timeframe_block_reason": block_reason,
        "timeframe_confidence_adjustment": round(confidence_adjustment, 4),
        "timeframe_notes": notes,
        "timeframe_weights": dict(weights),
    }
