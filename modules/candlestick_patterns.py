"""Detecção de padrões de candlestick — 4ª componente do scoring (shadow, peso 0).

`detect_patterns()` é a única função pública. Usa apenas candles **fechadas**
(quem chama é responsável por não incluir a candle em formação — ver
`modules.decision_engine.decide()`, que alimenta esta função a partir do
mesmo `MarketContext.candles_by_timeframe` usado tanto pelo live como pelo
backtest, garantindo um único caminho de cálculo).

Padrões implementados (apenas estes):
    bullish_engulfing / bearish_engulfing, hammer / shooting_star (pin bar),
    inside_bar, doji.

O ajuste de tendência D1 usa `modules.technical.ema()` (EMA20 vs EMA50 sobre
`d1_candles`) — não reimplementa EMA.
"""

from dataclasses import dataclass, field
from typing import List, Optional

import pandas as pd

from modules.technical import ema

# --- Parâmetros dos padrões (nomeados, nada de números mágicos inline) -----

ENGULFING_SCORE = 0.5
PIN_BAR_SCORE = 0.4

PIN_BAR_SHADOW_BODY_RATIO = 2.0      # sombra dominante >= 2x o corpo
PIN_BAR_MAX_BODY_RANGE_RATIO = 0.35  # corpo pequeno: <= 35% da range total
PIN_BAR_BODY_EDGE_RANGE_RATIO = 1 / 3  # corpo no terço oposto à sombra dominante

DOJI_BODY_RANGE_RATIO = 0.10  # corpo <= 10% da range total

D1_EMA_FAST = 20
D1_EMA_SLOW = 50
D1_MIN_CANDLES = D1_EMA_SLOW + 1
D1_TREND_EPSILON = 1e-6  # ignora ruído de ponto flutuante em séries ~planas

D1_ALIGNED_MULTIPLIER = 1.0
D1_AGAINST_MULTIPLIER = 0.25
D1_NEUTRAL_MULTIPLIER = 0.5  # tendência D1 neutra OU d1_candles indisponível


@dataclass
class PatternResult:
    pattern_score: float
    pattern_names: List[str] = field(default_factory=list)
    pattern_reason: str = ""
    d1_trend: str = "neutral"
    raw_score: float = 0.0


def _clamp(value, lo=-1.0, hi=1.0):
    return max(lo, min(hi, value))


def _body(candle):
    return abs(candle["close"] - candle["open"])


def _range(candle):
    return candle["high"] - candle["low"]


def _is_bullish(candle):
    return candle["close"] > candle["open"]


def _is_bearish(candle):
    return candle["close"] < candle["open"]


def _detect_engulfing(previous, current):
    """Corpo da candle actual engole totalmente o corpo da anterior, com
    direcções opostas. Devolve (nome, score) ou None."""
    if _is_bearish(previous) and _is_bullish(current):
        if current["open"] <= previous["close"] and current["close"] >= previous["open"]:
            return "bullish_engulfing", ENGULFING_SCORE
    if _is_bullish(previous) and _is_bearish(current):
        if current["open"] >= previous["close"] and current["close"] <= previous["open"]:
            return "bearish_engulfing", -ENGULFING_SCORE
    return None


def _detect_pin_bar(candle):
    """Pin bar (hammer/shooting star): sombra dominante >= 2x o corpo, corpo
    pequeno face à range, e localizado no terço oposto à sombra dominante.
    Devolve (nome, score) ou None."""
    rng = _range(candle)
    if rng <= 0:
        return None
    body = _body(candle)
    if body > PIN_BAR_MAX_BODY_RANGE_RATIO * rng:
        return None

    body_top = max(candle["open"], candle["close"])
    body_bottom = min(candle["open"], candle["close"])
    upper_shadow = candle["high"] - body_top
    lower_shadow = body_bottom - candle["low"]

    if body > 0 and lower_shadow >= PIN_BAR_SHADOW_BODY_RATIO * body and lower_shadow > upper_shadow:
        if (candle["high"] - body_top) <= PIN_BAR_BODY_EDGE_RANGE_RATIO * rng:
            return "hammer", PIN_BAR_SCORE
    if body > 0 and upper_shadow >= PIN_BAR_SHADOW_BODY_RATIO * body and upper_shadow > lower_shadow:
        if (body_bottom - candle["low"]) <= PIN_BAR_BODY_EDGE_RANGE_RATIO * rng:
            return "shooting_star", -PIN_BAR_SCORE
    return None


def _detect_inside_bar(previous, current):
    if current["high"] <= previous["high"] and current["low"] >= previous["low"]:
        return "inside_bar"
    return None


def _detect_doji(candle):
    rng = _range(candle)
    if rng <= 0:
        return None
    if _body(candle) <= DOJI_BODY_RANGE_RATIO * rng:
        return "doji"
    return None


def _d1_trend(d1_candles):
    if not d1_candles or len(d1_candles) < D1_MIN_CANDLES:
        return "neutral"
    closes = pd.Series([c["close"] for c in d1_candles])
    ema_fast = ema(closes, D1_EMA_FAST)
    ema_slow = ema(closes, D1_EMA_SLOW)
    last_fast = ema_fast.iloc[-1]
    last_slow = ema_slow.iloc[-1]
    if pd.isna(last_fast) or pd.isna(last_slow):
        return "neutral"
    delta = last_fast - last_slow
    if delta > D1_TREND_EPSILON:
        return "bullish"
    if delta < -D1_TREND_EPSILON:
        return "bearish"
    return "neutral"


def _trend_multiplier(raw_score, d1_trend):
    if raw_score == 0 or d1_trend == "neutral":
        return D1_NEUTRAL_MULTIPLIER
    pattern_direction = "bullish" if raw_score > 0 else "bearish"
    if pattern_direction == d1_trend:
        return D1_ALIGNED_MULTIPLIER
    return D1_AGAINST_MULTIPLIER


def detect_patterns(candles: list, d1_candles: Optional[list] = None) -> PatternResult:
    """Detecta padrões de candlestick nas últimas 2-3 candles **fechadas**.

    `candles` e `d1_candles`: listas de dicts com pelo menos as chaves
    open/high/low/close, ordenadas da mais antiga para a mais recente
    (a última entrada é a candle fechada mais recente). Nunca inclui a
    candle em formação — quem chama é responsável por esse corte.
    """
    if not candles or len(candles) < 2:
        return PatternResult(pattern_score=0.0, pattern_reason="candles insuficientes para padrões")

    current = candles[-1]
    previous = candles[-2]

    directional = []  # list of (name, contribution)
    context_names = []

    engulfing = _detect_engulfing(previous, current)
    if engulfing:
        directional.append(engulfing)

    pin_bar = _detect_pin_bar(current)
    if pin_bar:
        directional.append(pin_bar)

    inside_bar = _detect_inside_bar(previous, current)
    if inside_bar:
        context_names.append(inside_bar)

    doji = _detect_doji(current)
    if doji:
        context_names.append(doji)

    pattern_names = [name for name, _ in directional] + context_names

    if not directional and not context_names:
        return PatternResult(pattern_score=0.0, pattern_reason="sem padrões detectados")

    raw_score = _clamp(sum(contribution for _, contribution in directional))
    d1_trend = _d1_trend(d1_candles)
    multiplier = _trend_multiplier(raw_score, d1_trend)
    pattern_score = _clamp(round(raw_score * multiplier, 4))

    parts = [f"{name} ({contribution:+.1f})" for name, contribution in directional]
    parts.extend(context_names)
    reason = "; ".join(parts) if parts else "sem padrões direccionais"
    if directional:
        if multiplier == D1_ALIGNED_MULTIPLIER:
            alignment = f"alinhado com D1 {d1_trend}"
        elif multiplier == D1_AGAINST_MULTIPLIER:
            alignment = f"contra D1 {d1_trend}"
        else:
            alignment = f"D1 {d1_trend} (sem edge)"
        reason += f" | {alignment} (x{multiplier:.2f}) -> pattern_score={pattern_score:+.2f}"

    return PatternResult(
        pattern_score=pattern_score,
        pattern_names=pattern_names,
        pattern_reason=reason,
        d1_trend=d1_trend,
        raw_score=raw_score,
    )
