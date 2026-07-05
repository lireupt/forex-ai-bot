"""Motor de decisão puro — extraído de `main.py` (Passo 3 do backtest engine).

`decide(ctx: MarketContext) -> Decision` cobre toda a cadeia de decisão:
análise técnica multi-timeframe -> scoring combinado -> selecção do gating
signal -> filtro macro -> risk engine/risk -> gating operacional -> cooldown
-> signal persistence -> parâmetros da trade (quando permitida).

Não faz fetch, não escreve em DB, não imprime. Toda a história (trades,
decisões, eventos) tem de vir explicitamente no `MarketContext` — é isso
que permite ao mesmo motor servir tanto o live (`main.py`) como o
`backtest_runner` (Fase A), respeitando point-in-time estrito.
"""

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from modules import multi_timeframe, scoring
from modules import analytics_metrics
from modules.candlestick_patterns import PatternResult, detect_patterns
from modules.event_rules import event_is_whitelisted, parse_event_time
from modules.macro_filter import get_macro_risk
from modules.market import forex_market_state
from modules.operational import operational_state
from modules.pair_spec import PairSpec
from modules.risk import evaluate_trade as risk_evaluate_trade
from modules.technical import analyse as analyse_technical

TIMEFRAME_HOURS = {"1h": 1, "30m": 0.5, "15m": 0.25, "4h": 4, "1d": 24}
D1_PATTERN_LOOKBACK = 60  # >= candlestick_patterns.D1_MIN_CANDLES (EMA50 precisa de histórico)

DEFAULT_AI_RESULT = {
    "signal": "NEUTRAL",
    "confidence": 0,
    "status": "ok",
    "bias": "NEUTRAL",
    "confidence_adjustment": 0.0,
    "risk_adjustment": 0.0,
    "hold_off": False,
    "reasoning": "",
    "reason": "",
    "macro_context": "",
    "volatility_context": "",
    "news_sentiment": "",
}


def _env_bool(name, default):
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name, default):
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return int(value)


def _env_float(name, default):
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _pair_currencies(pair):
    base, quote = pair.replace(" ", "").upper().split("/")
    return {base, quote}


def _closed_candle_dicts(candles_df, count=3):
    """Últimas `count` candles fechadas de um DataFrame (open/high/low/close),
    mais antiga primeiro — formato de entrada de `candlestick_patterns.detect_patterns()`.
    `candles_by_timeframe` já vem filtrado point-in-time por quem constrói o
    `MarketContext` (live: cache de candles fechadas; backtest: `candles_up_to`
    com corte estrito `< t`), por isso não há filtragem adicional aqui."""
    if candles_df is None or candles_df.empty:
        return []
    tail = candles_df.tail(count)
    return [
        {
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
        }
        for _, row in tail.iterrows()
    ]


def parse_dt(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass
class MarketContext:
    pair: str
    timeframe: str
    now: datetime
    pair_spec: PairSpec
    candles_by_timeframe: Dict[str, Any]
    events: List[dict] = field(default_factory=list)
    high_impact_events: List[dict] = field(default_factory=list)
    ai_result: Optional[dict] = None
    scoring_config: Optional[dict] = None
    news_score: float = 0.0
    rolling_context_state: Optional[dict] = None
    recent_paper_trades: List[dict] = field(default_factory=list)
    last_closed_paper_trade: Optional[dict] = None
    recent_paper_trades_for_performance: List[dict] = field(default_factory=list)
    recent_decisions: List[dict] = field(default_factory=list)
    gating_mode: str = "score"
    sl_mult: Optional[float] = None
    tp_mult: Optional[float] = None
    expiry_bars: Optional[int] = None
    source: str = "combined"
    allow_buy: bool = True
    allow_sell: bool = True
    operational_now: Optional[datetime] = None
    operational_mode: str = "trade"
    operational_tolerance_minutes: int = 0


@dataclass
class Decision:
    technical_result: dict
    ai_result: dict
    combined: dict
    shadow_combined: dict
    gating_combined: dict
    gating_mode_used: str
    trade_decision: dict
    gate_context: dict
    event_risk: dict
    cooldown: dict
    signal_persistence: int
    risk_performance: dict
    macro_result: dict
    operational_state: dict
    market_state: dict
    current_price: Optional[float]
    atr_pips: Optional[float]
    ai_score: float
    technical_score: float
    shadow_score: float
    combined_score: float
    score_signal: str
    confidence_adjustment: float
    confidence_adjustment_reasons: list
    trade_params: Optional[dict]
    pattern_result: PatternResult

    @property
    def signal(self):
        return self.gating_combined.get("signal", "NEUTRAL")

    @property
    def trade_allowed(self):
        return bool(self.trade_decision.get("trade_allowed"))

    @property
    def blocking_reason(self):
        return _build_blocking_reason(self.combined, self.trade_decision)


def _direction_score(signal):
    if signal == "BUY":
        return 1.0
    if signal == "SELL":
        return -1.0
    return 0.0


def _ai_context_score(ai_result):
    bias = (ai_result.get("bias") or ai_result.get("signal") or "NEUTRAL").upper()
    adjustment = ai_result.get("confidence_adjustment")
    if adjustment is None:
        legacy_conf = scoring.confidence_to_unit(ai_result.get("confidence"))
        adjustment = legacy_conf * 0.20
    try:
        adjustment = float(adjustment)
    except (TypeError, ValueError):
        adjustment = 0.0
    # Usar apenas a magnitude; a direcção é sempre definida por `bias`.
    magnitude = min(0.25, abs(adjustment))
    return round(_direction_score(bias) * magnitude, 4)


def _ai_abstains(ai_result):
    """A IA abstém-se do score combinado quando tem baixa convicção — ver
    docstring original em main.py (histórico) para o racional completo."""
    if (ai_result.get("status") or "").lower() == "failed":
        return True
    try:
        conf = float(ai_result.get("confidence") or 0.0)
    except (TypeError, ValueError):
        conf = 0.0
    return conf < _env_float("AI_VOTE_MIN_CONFIDENCE", 35.0)


def _decision_confidence_adjustment(technical_result):
    alignment = technical_result.get("timeframe_alignment") or ""
    adjustment = 0.0
    reasons = []
    if "h1_h4_aligned" in alignment:
        adjustment += 0.10
        reasons.append("h1_h4_aligned:+0.10")
    if "m15_against" in alignment:
        adjustment -= 0.10
        reasons.append("m15_against_h1:-0.10")
    if alignment == "d1_strongly_against_h1":
        adjustment -= 0.20
        reasons.append("d1_strongly_against_h1:-0.20")
    if technical_result.get("timeframe_block_reason"):
        adjustment -= 0.20
        reasons.append("h4_d1_strongly_against_h1:-0.20")
    return round(adjustment, 4), reasons


def _neutral_reason(ai_score, technical_score, combined_score, ai_signal, technical_signal):
    if ai_signal != "NEUTRAL" and technical_signal != "NEUTRAL" and ai_signal != technical_signal:
        return "conflicting_signals"
    if abs(combined_score or 0.0) < 0.20:
        return "weak_signal"
    return "combined_score_below_threshold"


def _combine_signals(ai_result, technical_result, scoring_config=None, news_score=0.0,
                      pattern_result=None):
    ai_signal = ai_result.get("signal", "NEUTRAL")
    technical_signal = technical_result.get("signal", "NEUTRAL")
    reasoning = ai_result.get("reasoning", "")

    if ai_result.get("status") == "failed":
        return {
            "signal": "NEUTRAL",
            "confidence": 0,
            "hold_off": True,
            "reasoning": f"decision_skipped_ai_failed: {reasoning}",
            "agreement": False,
            "combined_score": 0.0,
            "components": {"ai_score": None, "technical_score": None, "news_score": news_score},
            "neutral_reason": "decision_skipped_ai_failed",
        }

    scoring_config = scoring_config or scoring.load_combined_scoring_config()
    indicators = technical_result.get("indicators", {})
    ai_score = _ai_context_score(ai_result)
    abstains = _ai_abstains(ai_result)
    ai_score_for_combine = None if abstains else ai_score

    technical_score = indicators.get("technical_score")
    if technical_score is None:
        technical_score = scoring.technical_votes_score(
            indicators.get("rsi_vote", indicators.get("rsi_signal", "neutral")),
            indicators.get("ema_vote", indicators.get("ema_trend", "neutral")),
            indicators.get("macd_vote", indicators.get("macd_signal", "neutral")),
        )
    pattern_score = (pattern_result.pattern_score if pattern_result else 0.0) or None
    combined_score = scoring.combine_scores(
        ai_score_for_combine,
        technical_score,
        news_score=news_score or None,
        pattern_score=pattern_score,
        config=scoring_config,
    )
    signal = scoring.score_to_signal(combined_score, scoring_config)
    timeframe_block_reason = technical_result.get("timeframe_block_reason") or ""
    confidence_adjustment, confidence_reasons = _decision_confidence_adjustment(technical_result)
    neutral_reason = ""
    if signal == "NEUTRAL":
        neutral_reason = _neutral_reason(
            ai_score, technical_score, combined_score, ai_signal, technical_signal
        )
    if timeframe_block_reason and signal != "NEUTRAL":
        reasoning = f"{reasoning} [timeframe_block={timeframe_block_reason}]"

    conf_val = float(ai_result.get("confidence") or 0.0)
    threshold_val = _env_float("AI_VOTE_MIN_CONFIDENCE", 35.0)
    if abstains:
        ai_vote_status = f"abstained:confidence={conf_val:.0f}<threshold={threshold_val:.0f}"
    else:
        ai_vote_status = f"included:confidence={conf_val:.0f}:score={ai_score:+.4f}"

    ai_w_eff = scoring_config["ai_weight"] if ai_score_for_combine is not None else 0.0
    tech_w_eff = scoring_config["technical_weight"]
    news_w_eff = scoring_config.get("news_weight", 0.0) if news_score else 0.0
    pattern_w_eff = scoring_config.get("pattern_weight", 0.0) if pattern_score is not None else 0.0
    total_w_eff = ai_w_eff + tech_w_eff + news_w_eff + pattern_w_eff
    effective_weights = {
        "ai": round(ai_w_eff / total_w_eff, 4) if total_w_eff > 0 else 0.0,
        "technical": round(tech_w_eff / total_w_eff, 4) if total_w_eff > 0 else 0.0,
        "news": round(news_w_eff / total_w_eff, 4) if total_w_eff > 0 else 0.0,
        "pattern": round(pattern_w_eff / total_w_eff, 4) if total_w_eff > 0 else 0.0,
    }

    return {
        "signal": signal,
        "confidence": max(0, min(100, int(round((abs(combined_score) + confidence_adjustment) * 100)))),
        "hold_off": bool(ai_result.get("hold_off", False)) or bool(timeframe_block_reason),
        "reasoning": reasoning,
        "agreement": ai_signal == technical_signal and ai_signal in ("BUY", "SELL"),
        "combined_score": combined_score,
        "components": {
            "ai_score": round(ai_score, 4),
            "ai_bias": ai_result.get("bias", ai_signal),
            "ai_confidence_adjustment": ai_result.get("confidence_adjustment", 0.0),
            "ai_risk_adjustment": ai_result.get("risk_adjustment", 0.0),
            "technical_score": round(float(technical_score or 0.0), 4),
            "news_score": round(float(news_score or 0.0), 4),
            "pattern_score": round(float(pattern_score or 0.0), 4),
            "technical_score_m15": technical_result.get("technical_score_m15"),
            "technical_score_h1": technical_result.get("technical_score_h1"),
            "technical_score_h4": technical_result.get("technical_score_h4"),
            "technical_score_d1": technical_result.get("technical_score_d1"),
            "multi_timeframe_score": technical_result.get("multi_timeframe_score"),
            "weights": {
                "ai": scoring_config["ai_weight"],
                "technical": scoring_config["technical_weight"],
                "news": scoring_config.get("news_weight", 0.0),
                "pattern": scoring_config.get("pattern_weight", 0.0),
            },
        },
        "neutral_reason": neutral_reason,
        "timeframe_block_reason": timeframe_block_reason,
        "confidence_adjustment": confidence_adjustment,
        "confidence_adjustment_reasons": confidence_reasons,
        "ai_vote_status": ai_vote_status,
        "effective_weights": effective_weights,
    }


def _shadow_combine(ai_result, technical_result):
    ai_signal = ai_result.get("signal", "NEUTRAL")
    shadow_signal = technical_result.get("shadow_technical_signal", "NEUTRAL")
    ai_conf = int(ai_result.get("confidence", 0) or 0)
    shadow_conf = int(technical_result.get("shadow_technical_confidence", 0) or 0)

    if ai_signal == "NEUTRAL" and shadow_signal == "NEUTRAL":
        return {"signal": "NEUTRAL", "confidence": 0, "reason": "ambos NEUTRAL"}

    if ai_signal == shadow_signal:
        return {
            "signal": ai_signal,
            "confidence": round((ai_conf + shadow_conf) / 2),
            "reason": "concordância IA + shadow técnica",
        }

    if ai_signal == "NEUTRAL" and shadow_signal in ("BUY", "SELL"):
        return {
            "signal": shadow_signal,
            "confidence": round(shadow_conf * 0.6),
            "reason": "apenas shadow técnica (IA NEUTRAL)",
        }

    if shadow_signal == "NEUTRAL" and ai_signal in ("BUY", "SELL"):
        return {
            "signal": ai_signal,
            "confidence": round(ai_conf * 0.6),
            "reason": "apenas IA (shadow NEUTRAL)",
        }

    return {
        "signal": "NEUTRAL",
        "confidence": 0,
        "reason": f"discordância IA ({ai_signal}) vs shadow ({shadow_signal})",
    }


def _select_gating_signal(strict_combined, score_signal, combined_score, shadow_combined, mode):
    """Devolve a versão de "combined" usada para o gating real.

    - "strict" (default): a regra 3/3 actual. Mantém o comportamento conservador.
    - "score": usa o score_combined_signal e |combined_score|*100 como confidence.
    - "shadow": usa shadow_combined (mistura IA + shadow técnica 2/3).
    """
    mode = (mode or "strict").strip().lower()
    if mode not in {"strict", "score", "shadow"}:
        mode = "strict"

    if mode == "score":
        confidence = int(round(abs(combined_score or 0) * 100))
        return {
            "signal": score_signal or "NEUTRAL",
            "confidence": confidence,
            "hold_off": bool(strict_combined.get("hold_off", True)),
            "reasoning": (strict_combined.get("reasoning", "") or "")
            + f" [gating=score, combined_score={(combined_score or 0):+.2f}]",
            "agreement": bool(strict_combined.get("agreement", False)),
        }, mode

    if mode == "shadow":
        return {
            "signal": shadow_combined.get("signal", "NEUTRAL") or "NEUTRAL",
            "confidence": int(shadow_combined.get("confidence", 0) or 0),
            "hold_off": bool(strict_combined.get("hold_off", True)),
            "reasoning": (shadow_combined.get("reason", "") or "") + " [gating=shadow]",
            "agreement": bool(strict_combined.get("agreement", False)),
        }, mode

    return dict(strict_combined), "strict"


def _build_blocking_reason(combined, trade_decision):
    block_reason = trade_decision.get("block_reason")
    if block_reason:
        return block_reason
    if combined.get("signal") == "NEUTRAL":
        return "sinal combinado é NEUTRAL"
    if combined.get("hold_off"):
        return "hold_off ativo"
    return ""


def aggregate_multi_timeframe_technical(candles_by_timeframe, pair):
    """Réplica pura de `main._get_multi_timeframe_technical`, sem a parte
    de fetch (I/O): recebe as candles já carregadas por timeframe-role e
    devolve o `technical_result` agregado + avisos."""
    technical_by_tf = {}
    warnings = []

    for key, candles in candles_by_timeframe.items():
        if candles is None or candles.empty:
            warnings.append(f"{key.upper()} sem candles; usado NEUTRAL 0.0")
            technical_by_tf[key] = analyse_technical(candles, pair, timeframe_role=key)
            continue
        technical_by_tf[key] = analyse_technical(candles, pair, timeframe_role=key)
        if (
            technical_by_tf[key].get("signal") == "NEUTRAL"
            and technical_by_tf[key].get("indicators", {}).get("technical_score") == 0.0
            and "Sem candles ou indicadores suficientes" in technical_by_tf[key].get("technical_reason", "")
        ):
            warnings.append(f"{key.upper()} com indicadores insuficientes; usado NEUTRAL 0.0")

    aggregate = multi_timeframe.aggregate(technical_by_tf)
    h1_result = dict(technical_by_tf.get("h1") or analyse_technical(None, pair, timeframe_role="h1"))
    h1_indicators = dict(h1_result.get("indicators") or {})
    h1_score = aggregate["technical_score_h1"]
    multi_score = aggregate["multi_timeframe_score"]
    multi_signal = aggregate["multi_timeframe_signal"]
    confidence = int(round(abs(multi_score) * 100 * aggregate["timeframe_confidence_adjustment"]))
    confidence = max(0, min(100, confidence))
    reason = (
        f"H1={h1_score:+.2f} continua principal; "
        f"M15={aggregate['technical_score_m15']:+.2f}, "
        f"H4={aggregate['technical_score_h4']:+.2f}, "
        f"D1={aggregate['technical_score_d1']:+.2f}; "
        f"multi_timeframe_score={multi_score:+.2f} -> {multi_signal}; "
        f"alinhamento={aggregate['timeframe_alignment']}."
    )
    if aggregate["timeframe_notes"]:
        reason += " " + "; ".join(aggregate["timeframe_notes"]) + "."
    if warnings:
        reason += " Avisos: " + "; ".join(warnings) + "."

    h1_indicators.update({
        "technical_score": multi_score,
        "technical_signal": multi_signal,
        "technical_score_m15": aggregate["technical_score_m15"],
        "technical_score_h1": aggregate["technical_score_h1"],
        "technical_score_h4": aggregate["technical_score_h4"],
        "technical_score_d1": aggregate["technical_score_d1"],
        "multi_timeframe_score": multi_score,
        "timeframe_alignment": aggregate["timeframe_alignment"],
        "timeframe_block_reason": aggregate["timeframe_block_reason"],
    })
    h1_result.update({
        "signal": multi_signal,
        "confidence": confidence,
        "technical_reason": reason,
        "indicators": h1_indicators,
        "timeframe_results": technical_by_tf,
        "timeframe_candle_counts": {
            tf: len(df) if df is not None else 0 for tf, df in candles_by_timeframe.items()
        },
        "timeframe_warnings": warnings,
        **aggregate,
    })
    return h1_result, warnings


def resolve_event_gate(high_impact_events, now_dt, window_minutes, relevant_currencies=None, enabled=True):
    """Réplica pura de `database.find_high_impact_event_nearby`, operando
    sobre uma lista de eventos já filtrados por alto impacto (em vez de
    consultar a tabela `economic_events` directamente), para respeitar
    point-in-time estrito no backtest."""
    if not enabled:
        return {
            "dangerous_event_nearby": False,
            "dangerous_event_reason": "",
            "event_gate_reason": "event_filter_disabled",
            "ignored_events": [],
        }

    relevant = None
    if relevant_currencies:
        relevant = {c.strip().upper() for c in relevant_currencies if c}

    ignored = []
    for row in sorted(high_impact_events, key=lambda r: r.get("event_time") or ""):
        event_time = parse_event_time(row.get("event_time"))
        if event_time is None:
            continue

        minutes = abs((event_time - now_dt).total_seconds()) / 60
        if minutes > window_minutes:
            continue

        title = row.get("title") or ""
        if not event_is_whitelisted(title):
            ignored.append({
                "reason": "event_ignored_not_whitelisted",
                "currency": row.get("country"),
                "title": title,
                "time": row.get("event_time"),
            })
            continue

        currency = (row.get("country") or "").strip().upper()
        if relevant is not None and currency and currency not in relevant:
            ignored.append({
                "reason": "event_ignored_wrong_currency",
                "currency": row.get("country"),
                "title": title,
                "time": row.get("event_time"),
            })
            continue

        direction = "daqui a" if event_time >= now_dt else "há"
        return {
            "dangerous_event_nearby": True,
            "dangerous_event_reason": (
                f"evento high impact {direction} {round(minutes)} min: "
                f"{row.get('country')} {row.get('title')}"
            ),
            "event_gate_reason": "high_impact_event_nearby",
            "event": {
                "currency": row.get("country"),
                "title": title,
                "time": row.get("event_time"),
                "source": row.get("source"),
                "minutes": round(minutes),
            },
            "ignored_events": ignored[-10:],
        }

    return {
        "dangerous_event_nearby": False,
        "dangerous_event_reason": "",
        "event_gate_reason": "",
        "ignored_events": ignored[-10:],
    }


def resolve_cooldown_config():
    enabled = _env_bool("COOLDOWN_ENABLED", True)
    max_per_day = _env_int("MAX_DIRECTION_SIGNALS_PER_DAY", _env_int("MAX_SIGNALS_PER_DIRECTION", 1))
    return {
        "enabled": enabled,
        "cooldown_minutes": _env_int("COOLDOWN_MINUTES", _env_int("COOLDOWN_AFTER_TRADE_HOURS", 2) * 60),
        "after_loss_hours": _env_int("COOLDOWN_AFTER_LOSS_HOURS", 3),
        "max_direction_signals_per_day": max_per_day,
    }


def _trades_since(trades, direction, cutoff_dt):
    result = []
    for trade in trades:
        if trade.get("direction") != direction:
            continue
        created_dt = parse_dt(trade.get("created_at"))
        if created_dt is None or created_dt < cutoff_dt:
            continue
        result.append(trade)
    return result


def cooldown_state(recent_trades, last_closed_trade, direction, now_dt, config):
    """Réplica pura de `main._cooldown_state`: `recent_trades` deve conter
    as trades (qualquer direcção) criadas desde, pelo menos,
    `now_dt - config['cooldown_minutes']` e desde o início do dia UTC."""
    state = {
        "cooldown_active": False,
        "max_direction_signals_reached": False,
        "reason": "",
        "config": config,
    }
    if not config["enabled"] or direction not in ("BUY", "SELL"):
        return state

    since_cooldown = now_dt - timedelta(minutes=config["cooldown_minutes"])
    recent_cooldown = _trades_since(recent_trades, direction, since_cooldown)
    if recent_cooldown:
        state["cooldown_active"] = True
        state["reason"] = "cooldown_active"
        state["recent_same_direction_count"] = len(recent_cooldown)
        return state

    day_start = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    today_same_direction = _trades_since(recent_trades, direction, day_start)
    if len(today_same_direction) >= config["max_direction_signals_per_day"]:
        state["max_direction_signals_reached"] = True
        state["reason"] = "max_direction_signals_reached"
        state["today_same_direction_count"] = len(today_same_direction)
        return state

    if last_closed_trade and last_closed_trade.get("status") == "loss":
        closed_dt = parse_dt(last_closed_trade.get("closed_at"))
        if closed_dt is not None:
            hours_since_loss = (now_dt - closed_dt).total_seconds() / 3600
            if hours_since_loss < config["after_loss_hours"]:
                state["cooldown_active"] = True
                state["reason"] = "cooldown_active"
                state["hours_since_loss"] = round(hours_since_loss, 2)
                return state

    return state


def signal_persistence_from_decisions(recent_decisions, direction, limit=5):
    """Réplica pura de `main._signal_persistence`. `recent_decisions` deve
    vir ordenada da mais recente para a mais antiga (como
    `ORDER BY id DESC`)."""
    if direction not in ("BUY", "SELL"):
        return 0
    rows = recent_decisions[:limit]
    count = 1
    for row in rows:
        previous = row.get("gating_signal") or row.get("combined_signal") or "NEUTRAL"
        if previous == direction:
            count += 1
            continue
        break
    return count


def risk_performance(recent_trades_for_performance, recent_decisions, pair, limit=30):
    """Réplica pura de `main._recent_risk_performance`.

    `recent_trades_for_performance` deve ser uma lista generosa (recomendado
    >= 200) das trades mais recentes (qualquer estado, `ORDER BY id DESC`),
    para garantir que as `limit` mais recentes FECHADAS (win/loss) coincidem
    com a query directa `WHERE status IN ('win','loss') ORDER BY id DESC
    LIMIT limit` que `calculate_analytics_metrics` corria antes."""
    batch = recent_trades_for_performance[:limit]
    closed = [t for t in batch if t.get("pair") == pair and t.get("status") in ("win", "loss")]
    loss_streak = 0
    for trade in closed:
        if trade.get("status") == "loss":
            loss_streak += 1
            continue
        break

    closed_for_metrics = [
        t for t in recent_trades_for_performance if t.get("status") in ("win", "loss")
    ][:limit]
    metrics = analytics_metrics.compute_metrics(closed_for_metrics, recent_decisions[:limit])
    return {
        "loss_streak": loss_streak,
        "max_drawdown": metrics.get("max_drawdown"),
        "winrate": metrics.get("winrate"),
        "expectancy": metrics.get("expectancy"),
    }


def compute_trade_levels(direction, current_price, atr_pips, pair_spec, created_at_dt,
                         timeframe, sl_mult=None, tp_mult=None, expiry_bars=None):
    """Réplica pura de `main._build_paper_trade` (só a matemática de
    entry/SL/TP/expiry — sem `decision_id`/`source`/`signal_source`, que
    são metadados de persistência acrescentados pelo chamador)."""
    if direction not in ("BUY", "SELL"):
        return None
    if current_price is None or current_price <= 0:
        return None

    sl_mult = pair_spec.sl_atr_mult if sl_mult is None else sl_mult
    tp_mult = pair_spec.tp_atr_mult if tp_mult is None else tp_mult
    expiry_bars = pair_spec.expiry_bars if expiry_bars is None else expiry_bars

    if atr_pips is None or atr_pips <= 0:
        atr_pips_used = 15.0
    else:
        atr_pips_used = float(atr_pips)

    sl_pips = round(atr_pips_used * sl_mult, 1)
    tp_pips = round(atr_pips_used * tp_mult, 1)
    atr_price = atr_pips_used * pair_spec.pip_size
    if direction == "BUY":
        sl = current_price - sl_pips * pair_spec.pip_size
        tp = current_price + tp_pips * pair_spec.pip_size
    else:
        sl = current_price + sl_pips * pair_spec.pip_size
        tp = current_price - tp_pips * pair_spec.pip_size

    bar_hours = TIMEFRAME_HOURS.get(timeframe, 1)
    expiry_dt = created_at_dt + timedelta(hours=bar_hours * expiry_bars)

    return {
        "direction": direction,
        "timeframe": timeframe,
        "entry_price": round(float(current_price), 5),
        "simulated_sl": round(float(sl), 5),
        "simulated_tp": round(float(tp), 5),
        "sl_pips": sl_pips,
        "tp_pips": tp_pips,
        "atr_pips": round(atr_pips_used, 1),
        "atr_price": round(atr_price, 5),
        "status": "open",
        "created_at": created_at_dt.isoformat(),
        "expiry_at": expiry_dt.isoformat(),
    }


def decide(ctx: MarketContext) -> Decision:
    scoring_config = ctx.scoring_config or scoring.load_combined_scoring_config()
    now_dt = ctx.now

    technical_result, _tf_warnings = aggregate_multi_timeframe_technical(
        ctx.candles_by_timeframe, ctx.pair
    )
    ai_result = ctx.ai_result if ctx.ai_result is not None else dict(DEFAULT_AI_RESULT)

    # Padrões de candlestick (Camada 4, shadow) — calculado aqui, dentro do
    # motor partilhado, a partir das mesmas candles fechadas que a análise
    # técnica já usa (`ctx.candles_by_timeframe`). Isto garante que live e
    # backtest nunca divergem: nenhum dos dois calcula padrões em separado.
    pattern_result = detect_patterns(
        _closed_candle_dicts(ctx.candles_by_timeframe.get("h1")),
        d1_candles=_closed_candle_dicts(ctx.candles_by_timeframe.get("d1"), count=D1_PATTERN_LOOKBACK),
    )

    combined = _combine_signals(
        ai_result, technical_result, scoring_config=scoring_config, news_score=ctx.news_score,
        pattern_result=pattern_result,
    )
    shadow_combined = _shadow_combine(ai_result, technical_result)
    current_price = technical_result.get("indicators", {}).get("current_price")
    atr_pips = technical_result.get("indicators", {}).get("atr_pips")

    ai_score_value = _ai_context_score(ai_result)
    technical_score_value = technical_result.get("indicators", {}).get("technical_score")
    if technical_score_value is None:
        technical_score_value = scoring.technical_votes_score(
            technical_result.get("indicators", {}).get(
                "rsi_vote", technical_result.get("indicators", {}).get("rsi_signal", "neutral")
            ),
            technical_result.get("indicators", {}).get(
                "ema_vote", technical_result.get("indicators", {}).get("ema_trend", "neutral")
            ),
            technical_result.get("indicators", {}).get(
                "macd_vote", technical_result.get("indicators", {}).get("macd_signal", "neutral")
            ),
        )
    shadow_score_value = scoring.signal_score(
        technical_result.get("shadow_technical_signal"),
        technical_result.get("shadow_technical_confidence"),
    )
    ai_voted = not _ai_abstains(ai_result)
    combined_score_value = scoring.combine_scores(
        ai_score_value if ai_voted else None, technical_score_value,
        shadow_score=shadow_score_value,
        news_score=ctx.news_score or None,
        pattern_score=pattern_result.pattern_score or None,
        config=scoring_config,
    )
    score_signal_value = scoring.score_to_signal(combined_score_value, scoring_config)
    confidence_adjustment, confidence_adjustment_reasons = _decision_confidence_adjustment(technical_result)

    gating_mode = (ctx.gating_mode or "score").strip().lower()
    gating_combined, gating_mode_used = _select_gating_signal(
        combined, score_signal_value, combined_score_value, shadow_combined, gating_mode,
    )

    event_window_minutes = _env_int("EVENT_BLOCK_WINDOW_MINUTES", 120)
    event_risk = resolve_event_gate(
        ctx.high_impact_events, now_dt, event_window_minutes,
        relevant_currencies=_pair_currencies(ctx.pair),
        enabled=_env_bool("EVENT_FILTER_ENABLED", True),
    )

    macro_result = get_macro_risk(ctx.pair, now_dt, events=ctx.events)

    if macro_result["macro_block"]:
        pass  # aplicado ao trade_decision mais abaixo
    elif macro_result["macro_risk_level"] == "medium":
        factor = _env_float("MACRO_MEDIUM_IMPACT_CONFIDENCE_FACTOR", 0.8)
        original_conf = gating_combined.get("confidence", 0) / 100.0
        adjusted_conf = original_conf * factor
        gating_combined = dict(gating_combined)
        gating_combined["confidence"] = int(round(adjusted_conf * 100))

    market_state = forex_market_state(now_utc=now_dt)
    op_state = operational_state(
        now_dt=ctx.operational_now,
        mode=ctx.operational_mode or "trade",
        tolerance_minutes=ctx.operational_tolerance_minutes,
    )

    cooldown_config = resolve_cooldown_config()
    cooldown_config["source"] = ctx.source
    cooldown_config["direction"] = gating_combined.get("signal")
    cooldown = cooldown_state(
        ctx.recent_paper_trades, ctx.last_closed_paper_trade,
        gating_combined.get("signal"), now_dt, cooldown_config,
    )
    signal_persistence = signal_persistence_from_decisions(
        ctx.recent_decisions, gating_combined.get("signal")
    )
    performance = risk_performance(
        ctx.recent_paper_trades_for_performance, ctx.recent_decisions, ctx.pair
    )

    gate_context = {
        "market": market_state,
        "cooldown": cooldown,
        "event": event_risk,
        "operational": op_state,
        "allow_buy": ctx.allow_buy,
        "allow_sell": ctx.allow_sell,
        "ai_status": ai_result.get("status", "ok"),
        "ai_signal": ai_result.get("signal"),
        "ai_confidence": ai_result.get("confidence"),
        "ai_confidence_score": scoring.confidence_to_unit(ai_result.get("confidence")),
        "ai_bias": ai_result.get("bias", ai_result.get("signal")),
        "ai_risk_adjustment": ai_result.get("risk_adjustment", 0.0),
        "confidence_adjustment": confidence_adjustment,
        "confidence_adjustment_reasons": confidence_adjustment_reasons,
        "technical_score": technical_score_value,
        "mtf_signal": technical_result.get("signal"),
        "multi_timeframe_score": technical_result.get("multi_timeframe_score"),
        "timeframe_alignment": technical_result.get("timeframe_alignment"),
        "timeframe_block_reason": technical_result.get("timeframe_block_reason"),
        "combined_score": combined_score_value,
        "neutral_reason": combined.get("neutral_reason"),
        "signal_persistence": signal_persistence,
        "performance": performance,
        "macro": macro_result,
    }
    trade_decision = risk_evaluate_trade(
        ctx.pair,
        gating_combined,
        current_price,
        event_risk,
        atr_pips=atr_pips,
        technical_indicators=technical_result.get("indicators", {}),
        gate_context=gate_context,
    )

    if macro_result["macro_block"]:
        trade_decision["trade_allowed"] = False
        trade_decision["simulated_order"] = None
        trade_decision["block_reason"] = "high_impact_macro_event"
        trade_decision.setdefault("gate_reasons", [])
        if "high_impact_macro_event" not in trade_decision["gate_reasons"]:
            trade_decision["gate_reasons"].append("high_impact_macro_event")

    trade_params = None
    final_signal = gating_combined.get("signal", "NEUTRAL")
    if trade_decision.get("trade_allowed") and final_signal in ("BUY", "SELL"):
        trade_params = compute_trade_levels(
            final_signal, current_price, atr_pips, ctx.pair_spec, ctx.now,
            ctx.timeframe, sl_mult=ctx.sl_mult, tp_mult=ctx.tp_mult, expiry_bars=ctx.expiry_bars,
        )

    return Decision(
        technical_result=technical_result,
        ai_result=ai_result,
        combined=combined,
        shadow_combined=shadow_combined,
        gating_combined=gating_combined,
        gating_mode_used=gating_mode_used,
        trade_decision=trade_decision,
        gate_context=gate_context,
        event_risk=event_risk,
        cooldown=cooldown,
        signal_persistence=signal_persistence,
        risk_performance=performance,
        macro_result=macro_result,
        operational_state=op_state,
        market_state=market_state,
        current_price=current_price,
        atr_pips=atr_pips,
        ai_score=ai_score_value,
        technical_score=technical_score_value,
        shadow_score=shadow_score_value,
        combined_score=combined_score_value,
        score_signal=score_signal_value,
        confidence_adjustment=confidence_adjustment,
        confidence_adjustment_reasons=confidence_adjustment_reasons,
        trade_params=trade_params,
        pattern_result=pattern_result,
    )
