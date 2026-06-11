import json
import os
import hashlib
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

from modules.ai_analyst import (
    analyse as analyse_ai,
    build_analysis_input,
    model_version_for_provider,
)
from modules import ai_aggregator
from modules import context_snapshot
from modules import database
from modules import rolling_context
from modules.macro_filter import get_macro_risk
from modules.market import forex_market_state, is_forex_market_open
from modules.weekly_market_prep import (
    weekend_mode_config,
    weekly_prep_config,
    is_weekend_mode_active,
    is_weekly_prep_due,
    run_weekly_prep,
)
from modules.news_scraper import fetch_all_events, fetch_all_news
from modules.operational import operational_state
from modules.price_feed import PROVIDER as PRICE_PROVIDER, fetch_candles
from modules.risk import evaluate_trade
from modules import scoring
from modules import multi_timeframe
from modules.technical import analyse as analyse_technical
from scripts.export_logs import export as export_web_data

load_dotenv()

PAIR = "EUR/USD"
TIMEFRAME = os.getenv("TIMEFRAME") or "1h"
TIMEFRAMES = {
    "m15": "15m",
    "h1": TIMEFRAME,
    "h4": "4h",
    "d1": "1d",
}
DECISIONS_LOG = Path("logs/decisions.jsonl")
PAPER_TRADE_DEFAULT_SL_MULT = 1.0
PAPER_TRADE_DEFAULT_TP_MULT = 2.0
PAPER_TRADE_DEFAULT_EXPIRY_BARS = 6
PIP_SIZE = 0.0001
TIMEFRAME_HOURS = {"1h": 1, "30m": 0.5, "15m": 0.25, "4h": 4, "1d": 24}


def _pair_currencies(pair):
    base, quote = pair.replace(" ", "").upper().split("/")
    return {base, quote}
INTERNET_CHECK_TARGETS = (
    ("www.google.com", 443),
    ("finance.yahoo.com", 443),
)


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


def _has_internet_connection(timeout=3):
    last_error = ""
    for host, port in INTERNET_CHECK_TARGETS:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True, f"{host}:{port}"
        except OSError as e:
            last_error = f"{host}:{port} -> {type(e).__name__}: {e}"
    return False, last_error


def _print_no_internet(reason):
    print("=== Forex AI Bot ===")
    print("Ligação à internet: indisponível")
    print(f"Motivo: {reason}")
    print()
    print("Pipeline cancelado para não poluir a base de dados nem os logs.")
    print("Nenhuma decisão foi gravada.")


def _load_cache_config():
    return {
        "use_cache": _env_bool("USE_CACHE", True),
        "force_refresh": _env_bool("FORCE_REFRESH", False),
        "news_cache_hours": _env_int("NEWS_CACHE_HOURS", 12),
        "calendar_cache_hours": _env_int("CALENDAR_CACHE_HOURS", 12),
        "price_cache_minutes": _env_int("PRICE_CACHE_MINUTES", 60),
        "ai_cache_daily": _env_bool("AI_CACHE_DAILY", True),
    }


def _cutoff(hours=0, minutes=0):
    return (datetime.now(timezone.utc) - timedelta(hours=hours, minutes=minutes)).isoformat()


def _format_blocked(status):
    if status == "blocked_js":
        return "BLOQUEADO (JS)"
    if status.startswith("blocked_"):
        return f"BLOQUEADO ({status.split('_', 1)[1]})"
    if status == "erro":
        return "ERRO"
    return status.upper()


def _format_scrape(state, unit):
    status = state.get("status", "")
    count = state.get("count", 0)
    if status == "ok":
        return f"OK ({count} {unit})"
    return _format_blocked(status)


def _format_api(state, with_sentiment=False):
    status = state.get("status", "")
    count = state.get("count", 0)
    if status == "ok":
        suffix = " (sentiment incluído)" if with_sentiment else ""
        return f"ok — {count} artigos{suffix}"
    if status == "sem key":
        return "sem key (PLACEHOLDER)"
    if status == "limite atingido":
        return "limite atingido (rate-limited)"
    if status == "erro":
        return "erro"
    return status


def _has_signal(state):
    return state.get("status") != "ok" or state.get("count", 0) > 0


def _print_sources(news_sources, event_sources):
    rows = []

    for feed_name, count in news_sources["rss_per_feed"].items():
        if count > 0:
            rows.append((f"RSS {feed_name}:", f"{count} artigos"))

    inv = news_sources["investing_html"]
    if _has_signal(inv):
        rows.append(("Investing HTML:", _format_scrape(inv, "artigos")))

    fxs = event_sources["fxstreet"]
    if _has_signal(fxs):
        rows.append(("FXStreet Calendar:", _format_scrape(fxs, "eventos")))

    if event_sources["rss_economic"] > 0:
        rows.append((
            "Eventos RSS económico:",
            f"{event_sources['rss_economic']} eventos",
        ))

    av = news_sources["alphavantage"]
    if _has_signal(av):
        rows.append(("Alpha Vantage API:", _format_api(av, with_sentiment=True)))

    mx = news_sources["marketaux"]
    if _has_signal(mx):
        rows.append(("Marketaux API:", _format_api(mx)))

    if not rows:
        return

    label_w = max(len(label) for label, _ in rows)
    print("┌─── FONTES ACTIVAS ────────────────────────")
    for label, value in rows:
        print(f"│  {label.ljust(label_w)}  {value}")
    print("└───────────────────────────────────────────")


def _cached_news_sources(count):
    return {
        "rss_per_feed": {"SQLite cache": count},
        "investing_html": {"status": "ok", "count": 0},
        "alphavantage": {"status": "ok", "count": 0},
        "marketaux": {"status": "ok", "count": 0},
    }


def _cached_event_sources(count):
    return {
        "rss_economic": count,
        "fxstreet": {"status": "ok", "count": 0},
    }


def _df_to_candles(df):
    candles = []
    for index, row in df.iterrows():
        candles.append({
            "candle_time": index.isoformat() if hasattr(index, "isoformat") else str(index),
            "open": row["open"],
            "high": row["high"],
            "low": row["low"],
            "close": row["close"],
            "volume": row["volume"],
        })
    return candles


def _candles_to_df(candles):
    if not candles:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    df = pd.DataFrame(candles)
    df["candle_time"] = pd.to_datetime(df["candle_time"])
    df = df.set_index("candle_time")
    return df[["open", "high", "low", "close", "volume"]]


def _get_news(conn, cache_config):
    if cache_config["use_cache"] and not cache_config["force_refresh"]:
        cached = database.get_recent_news(
            conn,
            PAIR,
            _cutoff(hours=cache_config["news_cache_hours"]),
        )
        if cached:
            return cached, _cached_news_sources(len(cached)), "cache"

    news, sources = fetch_all_news(with_sources=True)
    database.save_news_items(conn, news, PAIR)
    return news, sources, "fresh"


def _get_events(conn, cache_config):
    if cache_config["use_cache"] and not cache_config["force_refresh"]:
        cached = database.get_recent_events(
            conn,
            _cutoff(hours=cache_config["calendar_cache_hours"]),
        )
        if cached:
            return cached, _cached_event_sources(len(cached)), "cache"

    events, sources = fetch_all_events(with_sources=True)
    database.save_economic_events(conn, events)
    return events, sources, "fresh"


def _get_candles(conn, cache_config, count=100, timeframe=TIMEFRAME):
    if cache_config["use_cache"] and not cache_config["force_refresh"]:
        cached = database.get_recent_market_candles(
            conn,
            PAIR,
            timeframe,
            PRICE_PROVIDER,
            _cutoff(minutes=cache_config["price_cache_minutes"]),
            count,
        )
        if cached:
            return _candles_to_df(cached), "cache"

    candles = fetch_candles(pair=PAIR, timeframe=timeframe, count=count)
    if not candles.empty:
        database.save_market_candles(
            conn,
            _df_to_candles(candles),
            PAIR,
            timeframe,
            PRICE_PROVIDER,
        )
    return candles, "fresh"


def _get_multi_timeframe_technical(conn, cache_config, count=260):
    technical_by_tf = {}
    candles_by_tf = {}
    origins = {}
    warnings = []

    for key, timeframe in TIMEFRAMES.items():
        candles, origin = _get_candles(conn, cache_config, count=count, timeframe=timeframe)
        origins[key] = origin
        candles_by_tf[key] = candles
        if candles is None or candles.empty:
            warnings.append(f"{key.upper()} sem candles; usado NEUTRAL 0.0")
            technical_by_tf[key] = analyse_technical(candles, PAIR, timeframe_role=key)
            continue
        technical_by_tf[key] = analyse_technical(candles, PAIR, timeframe_role=key)
        if (
            technical_by_tf[key].get("signal") == "NEUTRAL"
            and technical_by_tf[key].get("indicators", {}).get("technical_score") == 0.0
            and "Sem candles ou indicadores suficientes" in technical_by_tf[key].get("technical_reason", "")
        ):
            warnings.append(f"{key.upper()} com indicadores insuficientes; usado NEUTRAL 0.0")

    aggregate = multi_timeframe.aggregate(technical_by_tf)
    h1_result = dict(technical_by_tf.get("h1") or analyse_technical(None, PAIR, timeframe_role="h1"))
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
        "timeframe_candle_status": origins,
        "timeframe_candle_counts": {tf: len(df) if df is not None else 0 for tf, df in candles_by_tf.items()},
        "timeframe_warnings": warnings,
        **aggregate,
    })
    return h1_result, candles_by_tf.get("h1"), origins, warnings


def _get_ai_result(
    conn,
    cache_config,
    provider,
    news,
    events,
    technical=None,
    macro_context_snapshot=None,
):
    input_text = build_analysis_input(
        news,
        events,
        PAIR,
        technical=technical,
        macro_context_snapshot=macro_context_snapshot,
    )
    input_hash = hashlib.sha256(input_text.encode("utf-8")).hexdigest()
    today = datetime.now(timezone.utc).date().isoformat()

    if cache_config["use_cache"] and cache_config["ai_cache_daily"] and not cache_config["force_refresh"]:
        cached = database.get_ai_analysis(conn, PAIR, today, input_hash, provider)
        if cached:
            return cached, input_hash, "cache"

    result = analyse_ai(
        news,
        events,
        PAIR,
        technical=technical,
        macro_context_snapshot=macro_context_snapshot,
    )
    if result.get("status") != "failed":
        database.save_ai_analysis(conn, PAIR, today, input_hash, result)
    return result, input_hash, "fresh"


def _neutral_reason(ai_score, technical_score, combined_score, ai_signal, technical_signal):
    if ai_signal != "NEUTRAL" and technical_signal != "NEUTRAL" and ai_signal != technical_signal:
        return "conflicting_signals"
    if abs(combined_score or 0.0) < 0.20:
        return "weak_signal"
    return "combined_score_below_threshold"


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
    # Sem abs(), um confidence_adjustment negativo + bias=SELL produzia dupla
    # negação e o score apontava para BUY: -1 × -0.10 = +0.10.
    magnitude = min(0.25, abs(adjustment))
    return round(_direction_score(bias) * magnitude, 4)


def _ai_abstains(ai_result):
    """A IA abstém-se do score combinado quando tem baixa convicção.

    Uma IA com confiança muito baixa (ex.: 5%) não deve diluir um sinal
    técnico limpo. Quando abstém, o peso da IA é retirado do `combine_scores`
    (renormalizado para a técnica) em vez de puxar o combinado para a zona
    neutra. Threshold em escala 0-100 via AI_VOTE_MIN_CONFIDENCE (default 35).
    """
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


def _combine_signals(ai_result, technical_result, scoring_config=None, news_score=0.0):
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

    scoring_config = scoring_config or scoring.load_scoring_config()
    indicators = technical_result.get("indicators", {})
    ai_score = _ai_context_score(ai_result)
    ai_score_for_combine = None if _ai_abstains(ai_result) else ai_score
    technical_score = indicators.get("technical_score")
    if technical_score is None:
        technical_score = scoring.technical_votes_score(
            indicators.get("rsi_vote", indicators.get("rsi_signal", "neutral")),
            indicators.get("ema_vote", indicators.get("ema_trend", "neutral")),
            indicators.get("macd_vote", indicators.get("macd_signal", "neutral")),
        )
    combined_score = scoring.combine_scores(
        ai_score_for_combine,
        technical_score,
        news_score=news_score or None,
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
            "technical_score_m15": technical_result.get("technical_score_m15"),
            "technical_score_h1": technical_result.get("technical_score_h1"),
            "technical_score_h4": technical_result.get("technical_score_h4"),
            "technical_score_d1": technical_result.get("technical_score_d1"),
            "multi_timeframe_score": technical_result.get("multi_timeframe_score"),
            "weights": {
                "ai": scoring_config["ai_weight"],
                "technical": scoring_config["technical_weight"],
                "news": scoring_config.get("news_weight", 0.0),
            },
        },
        "neutral_reason": neutral_reason,
        "timeframe_block_reason": timeframe_block_reason,
        "confidence_adjustment": confidence_adjustment,
        "confidence_adjustment_reasons": confidence_reasons,
    }


def _shadow_combine(ai_result, technical_result):
    ai_signal = ai_result.get("signal", "NEUTRAL")
    shadow_signal = technical_result.get("shadow_technical_signal", "NEUTRAL")
    ai_conf = int(ai_result.get("confidence", 0) or 0)
    shadow_conf = int(technical_result.get("shadow_technical_confidence", 0) or 0)

    if ai_signal == "NEUTRAL" and shadow_signal == "NEUTRAL":
        return {
            "signal": "NEUTRAL",
            "confidence": 0,
            "reason": "ambos NEUTRAL",
        }

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


def _decision_signature(technical_result, ai_result, combined, current_price):
    payload = {
        "tech": technical_result.get("signal"),
        "ai": ai_result.get("signal"),
        "combined": combined.get("signal"),
        "shadow": technical_result.get("shadow_technical_signal"),
        "price": round(current_price, 4) if current_price is not None else None,
    }
    blob = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _fmt(value, suffix=""):
    if value is None:
        return "n/a"
    return f"{value}{suffix}"


def _technical_details(technical_result):
    indicators = technical_result.get("indicators", {})
    ema20 = indicators.get("ema20")
    ema50 = indicators.get("ema50")
    macd = indicators.get("macd")
    macd_signal_value = indicators.get("macd_signal_value")

    if ema20 is None or ema50 is None:
        ema_relation = "n/a"
    else:
        ema_relation = "EMA20 > EMA50" if ema20 > ema50 else "EMA20 < EMA50"

    if macd is None or macd_signal_value is None:
        macd_relation = "n/a"
    else:
        macd_relation = "MACD > signal" if macd > macd_signal_value else "MACD < signal"

    return {
        "rsi_vote": indicators.get("rsi_vote") or indicators.get("rsi_signal", "neutral"),
        "ema_vote": indicators.get("ema_vote") or indicators.get("ema_trend", "neutral"),
        "macd_vote": indicators.get("macd_vote") or indicators.get("macd_signal", "neutral"),
        "rsi_value": indicators.get("rsi"),
        "ema20_value": ema20,
        "ema50_value": ema50,
        "macd_value": macd,
        "macd_signal_value": macd_signal_value,
        "adx_value": indicators.get("adx") if indicators.get("adx") is not None else indicators.get("adx14"),
        "atr14_value": indicators.get("atr14"),
        "atr_price": indicators.get("atr_price"),
        "atr_pips": indicators.get("atr_pips"),
        "volatility_reason": indicators.get("volatility_reason", ""),
        "ema_relation": ema_relation,
        "macd_relation": macd_relation,
        "technical_reason": technical_result.get("technical_reason", ""),
        "shadow_technical_signal": technical_result.get("shadow_technical_signal", "NEUTRAL"),
        "shadow_technical_confidence": technical_result.get("shadow_technical_confidence", 0),
        "shadow_technical_reason": technical_result.get("shadow_technical_reason", ""),
    }


def _print_final(ai_result, technical_result, combined, trade_decision):
    indicators = technical_result.get("indicators", {})
    details = _technical_details(technical_result)

    agreement_label = "← concordância" if combined["agreement"] else ""

    print("┌─── ANÁLISE TÉCNICA ───────────────────────")
    print(f"│  Sinal técnico:   {technical_result['signal']}")
    print(f"│  Confiança:       {technical_result['confidence']}%")
    print(f"│  RSI (14):        {_fmt(indicators.get('rsi'))}  → {indicators.get('rsi_signal', 'neutral')}")
    print(f"│  EMA trend:       {details['ema_vote']}  ({details['ema_relation']})")
    print(f"│  MACD:            {indicators.get('macd_signal', 'neutral')}")
    print(f"│  ATR (14):        {_fmt(details['atr_price'])}  ({_fmt(details['atr_pips'], ' pips')})")
    print(f"│  Preço actual:    {_fmt(indicators.get('current_price'))}")
    print("└───────────────────────────────────────────")

    print()
    print("┌─── ANÁLISE IA (fundamental) ──────────────")
    print(f"│  Sinal IA:        {ai_result['signal']}")
    print(f"│  Confiança:       {ai_result['confidence']}%")
    print(f"│  Raciocínio:      {combined['reasoning']}")
    print("└───────────────────────────────────────────")

    print()
    print("┌─── SINAL COMBINADO ────────────────────────")
    print(f"│  Sinal:           {combined['signal']:<12} {agreement_label}")
    print(f"│  Confiança final: {combined['confidence']}%")
    print(f"│  Hold off:        {combined['hold_off']}")
    print("│  Acção:           Avaliar DRY RUN")
    print("└────────────────────────────────────────────")

    print()
    print("┌─── ORDEM SIMULADA ─────────────────────────")
    if trade_decision["trade_allowed"]:
        order = trade_decision["simulated_order"]
        print(f"│  Modo:            {order['mode']}")
        print(f"│  Par:             {order['pair']}")
        print(f"│  Sinal:           {order['signal']}")
        print(f"│  Entrada:         {order['entry_price']}")
        print(f"│  Stop loss:       {order['stop_loss']} ({order['stop_loss_pips']} pips)")
        print(f"│  Take profit:     {order['take_profit']} ({order['take_profit_pips']} pips)")
        print(f"│  SL/TP mode:      {order['sl_tp_mode']} (ATR={order.get('atr_pips_used')})")
        print(f"│  Confiança:       {order['confidence']}%")
        print(f"│  Risco:           {order['risk_percent']}%")
        print(f"│  Tamanho estim.:  {order['estimated_position_size']} unidades")
        print(f"│  Razão:           {order['reason']}")
    else:
        print("│  Modo:            DRY_RUN")
        print("│  Trade:           BLOQUEADO")
        print(f"│  Motivo:          {trade_decision['block_reason']}")
        if trade_decision.get("dangerous_event_nearby"):
            print(f"│  Evento risco:    {trade_decision.get('dangerous_event_reason')}")
    print("└────────────────────────────────────────────")


def _build_features_snapshot(technical_result, candles_df, ai_result):
    indicators = technical_result.get("indicators", {})
    snapshot = {
        "close": indicators.get("current_price"),
        "rsi": indicators.get("rsi"),
        "ema20": indicators.get("ema20"),
        "ema50": indicators.get("ema50"),
        "ema20_minus_ema50": None,
        "macd": indicators.get("macd"),
        "macd_signal_value": indicators.get("macd_signal_value"),
        "macd_minus_signal": None,
        "atr14": indicators.get("atr14"),
        "atr_pips": indicators.get("atr_pips"),
        "adx": indicators.get("adx") if indicators.get("adx") is not None else indicators.get("adx14"),
        "volatility_level": _volatility_label(indicators.get("atr_pips")),
        "trend": indicators.get("ema_trend"),
        "momentum": indicators.get("macd_signal"),
        "ai_signal": ai_result.get("signal"),
        "ai_confidence": ai_result.get("confidence"),
        "ai_risk_level": ai_result.get("risk_level"),
        "ai_hold_off": bool(ai_result.get("hold_off")),
        "technical_score_m15": indicators.get("technical_score_m15"),
        "technical_score_h1": indicators.get("technical_score_h1"),
        "technical_score_h4": indicators.get("technical_score_h4"),
        "technical_score_d1": indicators.get("technical_score_d1"),
        "multi_timeframe_score": indicators.get("multi_timeframe_score"),
        "timeframe_alignment": indicators.get("timeframe_alignment"),
        "timeframe_block_reason": indicators.get("timeframe_block_reason"),
    }
    ema20 = indicators.get("ema20")
    ema50 = indicators.get("ema50")
    if ema20 is not None and ema50 is not None:
        snapshot["ema20_minus_ema50"] = round(float(ema20) - float(ema50), 5)
    macd_v = indicators.get("macd")
    macd_signal_v = indicators.get("macd_signal_value")
    if macd_v is not None and macd_signal_v is not None:
        snapshot["macd_minus_signal"] = round(float(macd_v) - float(macd_signal_v), 5)

    snapshot["recent_candles"] = _summarise_recent_candles(candles_df, n=5)
    snapshot["recent_change_pct"] = _recent_change_pct(candles_df, n=5)
    return snapshot


def _ai_reason(ai_result, combined=None):
    analysis_text = _ai_analysis_text(ai_result, combined=combined)
    sentences = _split_sentences(analysis_text)
    if sentences:
        return " ".join(sentences[:2])
    return analysis_text


def _ai_analysis_text(ai_result, combined=None, features_snapshot=None):
    for key in ("reasoning", "reason", "explanation", "analysis"):
        value = ai_result.get(key)
        if value:
            return str(value).strip()

    if combined:
        value = combined.get("reasoning")
        if value:
            return str(value).strip()

    if features_snapshot:
        return _fallback_ai_analysis(ai_result, features_snapshot)

    signal = ai_result.get("signal") or "NEUTRAL"
    confidence = ai_result.get("confidence")
    risk = ai_result.get("risk_level")
    parts = [f"IA devolveu {signal}"]
    if confidence is not None:
        parts.append(f"com confiança {confidence}%")
    if risk:
        parts.append(f"e risco {risk}")
    return " ".join(parts) + ". A resposta não incluiu raciocínio detalhado."


def _fallback_ai_analysis(ai_result, features_snapshot):
    signal = ai_result.get("signal") or features_snapshot.get("ai_signal") or "NEUTRAL"
    confidence = ai_result.get("confidence", features_snapshot.get("ai_confidence"))
    risk = ai_result.get("risk_level") or features_snapshot.get("ai_risk_level") or "n/a"
    close = features_snapshot.get("close")
    rsi = features_snapshot.get("rsi")
    trend = features_snapshot.get("trend")
    momentum = features_snapshot.get("momentum")
    atr_pips = features_snapshot.get("atr_pips")
    volatility = features_snapshot.get("volatility_level")
    recent_change = features_snapshot.get("recent_change_pct")
    return (
        f"IA devolveu {signal} com confiança {confidence}% e risco {risk}. "
        f"Snapshot usado: preço={_fmt(close)}, RSI={_fmt(rsi)}, tendência EMA={trend or 'n/a'}, "
        f"momentum MACD={momentum or 'n/a'}, ATR={_fmt(atr_pips, ' pips')} "
        f"({volatility or 'unknown'}), variação recente={_fmt(recent_change, '%')}. "
        "A resposta do provider não incluiu uma narrativa detalhada, por isso esta análise foi gerada deterministicamente a partir dos inputs registados."
    )


def _split_sentences(text):
    if not text:
        return []
    sentences = []
    current = []
    for char in str(text).strip():
        current.append(char)
        if char in ".!?":
            sentence = "".join(current).strip()
            if sentence:
                sentences.append(sentence)
            current = []
    tail = "".join(current).strip()
    if tail:
        sentences.append(tail)
    return sentences


def _ai_model_version(ai_result, provider):
    explicit = ai_result.get("model_version") or ai_result.get("model")
    if explicit:
        return str(explicit)
    return model_version_for_provider(ai_result.get("provider") or provider)


def _volatility_label(atr_pips):
    if atr_pips is None:
        return "unknown"
    try:
        value = float(atr_pips)
    except (TypeError, ValueError):
        return "unknown"
    if value < 8:
        return "low"
    if value <= 20:
        return "normal"
    return "high"


def _summarise_recent_candles(candles_df, n=5):
    if candles_df is None or candles_df.empty:
        return []
    tail = candles_df.tail(n)
    summary = []
    for index, row in tail.iterrows():
        summary.append({
            "time": index.isoformat() if hasattr(index, "isoformat") else str(index),
            "open": round(float(row["open"]), 5),
            "high": round(float(row["high"]), 5),
            "low": round(float(row["low"]), 5),
            "close": round(float(row["close"]), 5),
        })
    return summary


def _recent_change_pct(candles_df, n=5):
    if candles_df is None or candles_df.empty:
        return None
    tail = candles_df.tail(n)
    if len(tail) < 2:
        return None
    first_close = float(tail.iloc[0]["close"])
    last_close = float(tail.iloc[-1]["close"])
    if first_close == 0:
        return None
    return round((last_close - first_close) / first_close * 100, 4)


def _build_combined_reason(ai_result, technical_result, combined, score_combined_signal,
                           ai_score, technical_score, combined_score):
    ai_signal = ai_result.get("signal", "NEUTRAL")
    ai_bias = ai_result.get("bias", ai_signal)
    tech_signal = technical_result.get("signal", "NEUTRAL")
    base = (
        f"IA contexto={ai_bias} (ajuste score {ai_score:+.2f}); "
        f"técnica={tech_signal} (score {technical_score:+.2f}); "
        f"score combinado={combined_score:+.2f} -> {score_combined_signal}."
    )
    components = combined.get("components") or {}
    weights = components.get("weights") or {}
    if weights:
        base += (
            f" Pesos: AI={weights.get('ai', 0):.2f}, "
            f"técnica={weights.get('technical', 0):.2f}, "
            f"news={weights.get('news', 0):.2f}."
        )
    mtf = technical_result.get("multi_timeframe_score")
    if mtf is not None:
        base += (
            f" Multi-TF={float(mtf):+.2f} "
            f"(M15={float(technical_result.get('technical_score_m15') or 0):+.2f}, "
            f"H1={float(technical_result.get('technical_score_h1') or 0):+.2f}, "
            f"H4={float(technical_result.get('technical_score_h4') or 0):+.2f}, "
            f"D1={float(technical_result.get('technical_score_d1') or 0):+.2f}); "
            f"alinhamento={technical_result.get('timeframe_alignment') or 'n/a'}."
        )
    if technical_result.get("timeframe_block_reason"):
        base += f" Timeframe block: {technical_result.get('timeframe_block_reason')}."
    if combined.get("agreement"):
        base += " Concordância IA/técnica."
    elif combined.get("neutral_reason"):
        base += f" NEUTRAL reason: {combined.get('neutral_reason')}."
    elif ai_signal == "NEUTRAL" or tech_signal == "NEUTRAL":
        base += " Pelo menos um componente é NEUTRAL; regra estrita antiga ficaria NEUTRAL."
    elif ai_signal != tech_signal:
        base += " Discordância IA vs técnica."
    return base


def _build_blocking_reason(combined, trade_decision):
    block_reason = trade_decision.get("block_reason")
    if block_reason:
        return block_reason
    if combined.get("signal") == "NEUTRAL":
        return "sinal combinado é NEUTRAL"
    if combined.get("hold_off"):
        return "hold_off ativo"
    return ""


def _select_gating_signal(strict_combined, score_signal, combined_score,
                          shadow_combined, mode):
    """Devolve a versão de "combined" usada para o gating real.

    - "strict" (default): a regra 3/3 actual. Mantém o comportamento conservador.
    - "score": usa o score_combined_signal e |combined_score|*100 como confidence.
      Bom quando os paper trades já validaram o threshold.
    - "shadow": usa shadow_combined (mistura IA + shadow técnica 2/3).

    Em todos os modos respeitamos `hold_off` da IA — ele só fica True quando
    há eventos imminent ou contradições fortes.
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
            "reasoning": (shadow_combined.get("reason", "") or "")
            + " [gating=shadow]",
            "agreement": bool(strict_combined.get("agreement", False)),
        }, mode

    return dict(strict_combined), "strict"


def _build_paper_trade(decision_id, pair, timeframe, direction, current_price,
                       atr_pips, source, signal_source, created_at_dt):
    if direction not in ("BUY", "SELL"):
        return None
    if current_price is None or current_price <= 0:
        return None

    sl_mult = float(os.getenv("PAPER_TRADE_SL_MULT") or PAPER_TRADE_DEFAULT_SL_MULT)
    tp_mult = float(os.getenv("PAPER_TRADE_TP_MULT") or PAPER_TRADE_DEFAULT_TP_MULT)
    expiry_bars = int(float(os.getenv("PAPER_TRADE_EXPIRY_BARS") or PAPER_TRADE_DEFAULT_EXPIRY_BARS))

    if atr_pips is None or atr_pips <= 0:
        atr_pips_used = 15.0
    else:
        atr_pips_used = float(atr_pips)

    sl_pips = round(atr_pips_used * sl_mult, 1)
    tp_pips = round(atr_pips_used * tp_mult, 1)
    atr_price = atr_pips_used * PIP_SIZE
    if direction == "BUY":
        sl = current_price - sl_pips * PIP_SIZE
        tp = current_price + tp_pips * PIP_SIZE
    else:
        sl = current_price + sl_pips * PIP_SIZE
        tp = current_price - tp_pips * PIP_SIZE

    bar_hours = TIMEFRAME_HOURS.get(timeframe, 1)
    expiry_dt = created_at_dt + timedelta(hours=bar_hours * expiry_bars)

    return {
        "decision_id": decision_id,
        "pair": pair,
        "timeframe": timeframe,
        "direction": direction,
        "entry_price": round(float(current_price), 5),
        "simulated_sl": round(float(sl), 5),
        "simulated_tp": round(float(tp), 5),
        "sl_pips": sl_pips,
        "tp_pips": tp_pips,
        "atr_pips": round(atr_pips_used, 1),
        "atr_price": round(atr_price, 5),
        "status": "open",
        "source": source,
        "signal_source": signal_source,
        "created_at": created_at_dt.isoformat(),
        "expiry_at": expiry_dt.isoformat(),
    }


def _build_decision_entry(
    pair,
    timeframe,
    source_status,
    ai_result,
    technical_result,
    combined,
    shadow_combined,
    trade_decision,
    signature,
    candles_df,
    provider,
    scoring_config,
    ai_score=None,
    technical_score=None,
    shadow_score=None,
    combined_score=None,
    score_combined_signal=None,
    gating_mode="strict",
    gating_signal=None,
    gating_confidence=None,
    operational_state=None,
):
    indicators = technical_result.get("indicators", {})
    details = _technical_details(technical_result)
    order = trade_decision.get("simulated_order") or {}

    if ai_score is None:
        ai_score = scoring.signal_score(
            ai_result.get("signal"), ai_result.get("confidence")
        )
    if technical_score is None:
        technical_score = scoring.technical_votes_score(
            details["rsi_vote"], details["ema_vote"], details["macd_vote"]
        )
    if shadow_score is None:
        shadow_score = scoring.signal_score(
            details["shadow_technical_signal"],
            details["shadow_technical_confidence"],
        )
    if combined_score is None:
        _ai_for_combine = None if _ai_abstains(ai_result) else ai_score
        combined_score = scoring.combine_scores(
            _ai_for_combine, technical_score, shadow_score=shadow_score, config=scoring_config
        )
    if score_combined_signal is None:
        score_combined_signal = scoring.score_to_signal(combined_score, scoring_config)

    ai_features_snapshot = _build_features_snapshot(technical_result, candles_df, ai_result)

    ai_analysis_text = _ai_analysis_text(
        ai_result,
        combined=combined,
        features_snapshot=ai_features_snapshot,
    )
    ai_reason = _ai_reason(
        {**ai_result, "reasoning": ai_analysis_text},
        combined,
    )

    combined_reason = _build_combined_reason(
        ai_result, technical_result, combined, score_combined_signal,
        ai_score, technical_score, combined_score,
    )

    blocking_reason = _build_blocking_reason(combined, trade_decision)

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "pair": pair,
        "timeframe": timeframe,
        "news_source_status": source_status["news"],
        "calendar_source_status": source_status["calendar"],
        "ai_source_status": source_status["ai"],
        "candles_source_status": source_status["candles"],
        "rsi_vote": details["rsi_vote"],
        "ema_vote": details["ema_vote"],
        "macd_vote": details["macd_vote"],
        "rsi_value": details["rsi_value"],
        "ema20_value": details["ema20_value"],
        "ema50_value": details["ema50_value"],
        "macd_value": details["macd_value"],
        "macd_signal_value": details["macd_signal_value"],
        "atr14_value": details["atr14_value"],
        "atr_price": details["atr_price"],
        "atr_pips": details["atr_pips"],
        "adx_value": details.get("adx_value"),
        "volatility_reason": details["volatility_reason"],
        "technical_reason": details["technical_reason"],
        "shadow_technical_signal": details["shadow_technical_signal"],
        "shadow_technical_confidence": details["shadow_technical_confidence"],
        "shadow_technical_reason": details["shadow_technical_reason"],
        "shadow_combined_signal": shadow_combined.get("signal"),
        "shadow_combined_confidence": shadow_combined.get("confidence"),
        "shadow_combined_reason": shadow_combined.get("reason"),
        "technical_signal": technical_result.get("signal"),
        "ai_signal": ai_result.get("signal"),
        "combined_signal": combined.get("signal"),
        "confidence": combined.get("confidence"),
        "hold_off": combined.get("hold_off"),
        "current_price": indicators.get("current_price"),
        "trade_allowed": trade_decision.get("trade_allowed"),
        "block_reason": trade_decision.get("block_reason"),
        "simulated_order": trade_decision.get("simulated_order"),
        "dangerous_event_nearby": trade_decision.get("dangerous_event_nearby"),
        "dangerous_event_reason": trade_decision.get("dangerous_event_reason"),
        "decision_signature": signature,
        "decision_hash": signature,
        "stop_loss_pips_used": order.get("stop_loss_pips"),
        "take_profit_pips_used": order.get("take_profit_pips"),
        "sl_tp_mode": order.get("sl_tp_mode"),
        "ai_score": round(ai_score, 4),
        "ai_confidence_score": round(scoring.confidence_to_unit(ai_result.get("confidence")), 4),
        "ai_analysis_text": ai_analysis_text,
        "ai_reason": ai_reason,
        "ai_features_snapshot": ai_features_snapshot,
        "ai_model_version": _ai_model_version(ai_result, provider),
        "ai_bias": ai_result.get("bias", ai_result.get("signal", "NEUTRAL")),
        "ai_confidence_adjustment": ai_result.get("confidence_adjustment", 0.0),
        "ai_risk_adjustment": ai_result.get("risk_adjustment", 0.0),
        "macro_context": ai_result.get("macro_context", ""),
        "volatility_context": ai_result.get("volatility_context", ""),
        "news_sentiment": ai_result.get("news_sentiment", ""),
        "ai_context_reason": ai_result.get("reason", ai_result.get("reasoning", "")),
        "technical_score": round(technical_score, 4),
        "technical_score_m15": technical_result.get("technical_score_m15"),
        "technical_score_h1": technical_result.get("technical_score_h1"),
        "technical_score_h4": technical_result.get("technical_score_h4"),
        "technical_score_d1": technical_result.get("technical_score_d1"),
        "multi_timeframe_score": technical_result.get("multi_timeframe_score"),
        "timeframe_alignment": technical_result.get("timeframe_alignment"),
        "timeframe_block_reason": technical_result.get("timeframe_block_reason"),
        "shadow_score": round(shadow_score, 4),
        "combined_score": combined_score,
        "combined_reason": combined_reason,
        "blocking_reason": blocking_reason,
        "score_combined_signal": score_combined_signal,
        "gate_diagnostics": trade_decision.get("gate_diagnostics"),
        "ai_status": ai_result.get("status", "ok"),
        "neutral_reason": combined.get("neutral_reason"),
        "gating_mode": gating_mode,
        "gating_signal": gating_signal or combined.get("signal"),
        "gating_confidence": gating_confidence if gating_confidence is not None else combined.get("confidence"),
        "operational_mode": (operational_state or {}).get("mode"),
        "operational_can_trade": (operational_state or {}).get("can_open_trade"),
        "operational_block_reason": (operational_state or {}).get("block_reason"),
    }


def _append_decision_log(entry):
    DECISIONS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with DECISIONS_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _save_jsonl(entry):
    try:
        _append_decision_log(entry)
        return True, ""
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _save_sqlite_decision(conn, entry):
    try:
        decision_id = database.save_decision(conn, entry)
        return True, "", decision_id
    except Exception as e:
        return False, f"{type(e).__name__}: {e}", None


def _maybe_create_paper_trade(conn, decision_id, pair, timeframe, ai_result,
                              combined, current_price, atr_pips, created_at_dt,
                              trade_decision=None):
    if decision_id is None:
        return None
    if trade_decision is not None and not trade_decision.get("trade_allowed"):
        return None

    combined_signal = combined.get("signal", "NEUTRAL")

    if combined_signal in ("BUY", "SELL"):
        direction = combined_signal
        source = "combined"
        signal_source = "combined_signal"
    else:
        return None

    paper_trade = _build_paper_trade(
        decision_id=decision_id,
        pair=pair,
        timeframe=timeframe,
        direction=direction,
        current_price=current_price,
        atr_pips=atr_pips,
        source=source,
        signal_source=signal_source,
        created_at_dt=created_at_dt,
    )
    if paper_trade is None:
        return None

    try:
        paper_trade_id = database.create_paper_trade(conn, paper_trade)
        database.link_decision_to_paper_trade(conn, decision_id, paper_trade_id)
        return paper_trade_id
    except Exception as e:
        print(f"[paper-trade] falha ao criar trade: {type(e).__name__}: {e}")
        return None


def _yes_no(value):
    return "yes" if value else "no"


def _print_run_summary(
    source_status,
    ai_result,
    technical_result,
    combined,
    trade_decision,
    jsonl_saved,
    sqlite_saved,
    is_duplicate=False,
    jsonl_error="",
    sqlite_error="",
):
    print()
    print("┌─── RESUMO DA EXECUÇÃO ─────────────────────")
    print("│  Fontes:")
    print(f"│    news:      {source_status['news']}")
    print(f"│    calendar:  {source_status['calendar']}")
    print(f"│    AI:        {source_status['ai']}")
    print(f"│    candles:   {source_status['candles']}")
    print("│  Sinais:")
    print(f"│    AI:        {ai_result.get('signal')} ({ai_result.get('confidence')}%)")
    print(f"│    técnico:   {technical_result.get('signal')} ({technical_result.get('confidence')}%)")
    print(f"│    combinado: {combined.get('signal')} ({combined.get('confidence')}%)")
    print(f"│    hold_off:  {combined.get('hold_off')}")
    details = _technical_details(technical_result)
    print("│  Technical:")
    print(f"│    RSI:       {details['rsi_vote']} ({_fmt(details['rsi_value'])})")
    print(f"│    EMA:       {details['ema_vote']} ({details['ema_relation']})")
    print(f"│    MACD:      {details['macd_vote']} ({details['macd_relation']})")
    print(f"│    ATR14:     {_fmt(details['atr_price'])} ({_fmt(details['atr_pips'], ' pips')})")
    print(f"│    Volatility:{' ' if details['volatility_reason'] else ''}{details['volatility_reason']}")
    print(f"│    Strict Final: {technical_result.get('signal')}")
    print(f"│    Shadow Final: {details['shadow_technical_signal']}")
    print(f"│    Shadow Reason: {details['shadow_technical_reason']}")
    print(f"│    Reason:    {details['technical_reason']}")
    print("│  Risco:")
    print(f"│    trade allowed: {_yes_no(trade_decision.get('trade_allowed'))}")
    if trade_decision.get("block_reason"):
        print(f"│    block reason:  {trade_decision.get('block_reason')}")
    if trade_decision.get("simulated_order"):
        order = trade_decision["simulated_order"]
        print(
            "│    order:         "
            f"{order['signal']} {order['pair']} @ {order['entry_price']} "
            f"SL {order['stop_loss']} TP {order['take_profit']}"
        )
    print(f"│    event nearby:  {_yes_no(trade_decision.get('dangerous_event_nearby'))}")
    if trade_decision.get("dangerous_event_reason"):
        print(f"│    event reason:  {trade_decision.get('dangerous_event_reason')}")
    print("│  Logs:")
    print(f"│    JSONL saved:   {_yes_no(jsonl_saved)} {jsonl_error}")
    print(f"│    SQLite saved:  {_yes_no(sqlite_saved)} {sqlite_error}")
    print(f"│    Duplicate:     {_yes_no(is_duplicate)}")
    print("└────────────────────────────────────────────")


def _print_recent_decisions(rows):
    if not rows:
        return

    print()
    print("┌─── DECISÕES RECENTES ──────────────────────")
    for row in rows:
        allowed = "simulado" if row["trade_allowed"] else "bloqueado"
        reason = row["block_reason"] or "-"
        print(
            f"│  {row['timestamp']}  {row['pair']}  "
            f"{row['combined_signal']} {row['confidence']}%  {allowed}  {reason}"
        )
    print("└────────────────────────────────────────────")


def _is_recent_duplicate(current_sig, last_sig, last_timestamp, window_minutes):
    if not current_sig or not last_sig or current_sig != last_sig:
        return False
    if not last_timestamp:
        return False
    try:
        last_dt = datetime.fromisoformat(last_timestamp.replace("Z", "+00:00"))
    except ValueError:
        return False
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=timezone.utc)
    age_minutes = (datetime.now(timezone.utc) - last_dt).total_seconds() / 60
    return age_minutes <= window_minutes


def _cooldown_state(conn, pair, source, direction, now_dt=None):
    enabled = _env_bool("COOLDOWN_ENABLED", True)
    now_dt = now_dt or datetime.now(timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    now_dt = now_dt.astimezone(timezone.utc)
    max_per_day = _env_int(
        "MAX_DIRECTION_SIGNALS_PER_DAY",
        _env_int("MAX_SIGNALS_PER_DIRECTION", 1),
    )
    config = {
        "enabled": enabled,
        "cooldown_minutes": _env_int(
            "COOLDOWN_MINUTES",
            _env_int("COOLDOWN_AFTER_TRADE_HOURS", 2) * 60,
        ),
        "after_loss_hours": _env_int("COOLDOWN_AFTER_LOSS_HOURS", 3),
        "max_direction_signals_per_day": max_per_day,
        "source": source,
        "direction": direction,
    }
    state = {
        "cooldown_active": False,
        "max_direction_signals_reached": False,
        "reason": "",
        "config": config,
    }
    if not enabled or direction not in ("BUY", "SELL"):
        return state

    since_cooldown = (now_dt - timedelta(minutes=config["cooldown_minutes"])).isoformat()
    recent_cooldown = database.get_recent_paper_trades_for_direction(
        conn, pair, source, direction, since_cooldown
    )
    if recent_cooldown:
        state["cooldown_active"] = True
        state["reason"] = "cooldown_active"
        state["recent_same_direction_count"] = len(recent_cooldown)
        return state

    day_start = now_dt.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    today_same_direction = database.get_recent_paper_trades_for_direction(
        conn, pair, source, direction, day_start
    )
    if len(today_same_direction) >= config["max_direction_signals_per_day"]:
        state["max_direction_signals_reached"] = True
        state["reason"] = "max_direction_signals_reached"
        state["today_same_direction_count"] = len(today_same_direction)
        return state

    last_closed = database.get_last_closed_paper_trade(conn, pair, source=source)
    if last_closed and last_closed.get("status") == "loss":
        closed_at = last_closed.get("closed_at")
        try:
            closed_dt = datetime.fromisoformat(str(closed_at).replace("Z", "+00:00"))
        except ValueError:
            closed_dt = None
        if closed_dt is not None:
            if closed_dt.tzinfo is None:
                closed_dt = closed_dt.replace(tzinfo=timezone.utc)
            hours_since_loss = (now_dt - closed_dt).total_seconds() / 3600
            if hours_since_loss < config["after_loss_hours"]:
                state["cooldown_active"] = True
                state["reason"] = "cooldown_active"
                state["hours_since_loss"] = round(hours_since_loss, 2)
                return state

    return state


def _signal_persistence(conn, pair, direction, limit=5):
    if direction not in ("BUY", "SELL"):
        return 0
    rows = database.get_recent_gating_decisions(conn, pair, limit=limit)
    count = 1
    for row in rows:
        previous = row.get("gating_signal") or row.get("combined_signal") or "NEUTRAL"
        if previous == direction:
            count += 1
            continue
        break
    return count


def _run_aggregator_shadow(
    conn,
    technical_result,
    ai_result,
    combined,
    gating_combined,
    trade_decision,
    gate_context,
    event_risk,
    risk_performance,
    gating_mode,
):
    """Camada 4 (IA agregadora) em modo SHADOW.

    Calcula o parecer agregado mas NÃO influencia a decisão nem o gating. É
    sempre não-fatal: qualquer falha devolve None e o ciclo segue normal.
    Activado por AI_AGGREGATOR_ENABLED (default off).
    """
    if not _env_bool("AI_AGGREGATOR_ENABLED", False):
        return None, None
    try:
        performance = context_snapshot.build_performance_snapshot(
            conn, PAIR, recent_performance=risk_performance
        )
        latest_prep = None
        try:
            latest_prep = database.get_latest_weekly_market_prep(conn, PAIR)
        except Exception:
            pass
        latest_rolling = None
        try:
            latest_rolling = database.get_latest_rolling_market_context(conn, PAIR)
        except Exception:
            pass
        snapshot = context_snapshot.build_market_snapshot(
            PAIR,
            technical_result,
            ai_result,
            combined,
            gating_combined,
            trade_decision,
            gate_context,
            event_risk,
            performance,
            gating_mode=gating_mode,
            latest_weekly_market_prep=latest_prep,
            latest_rolling_context=latest_rolling,
        )
        result = ai_aggregator.analyse(snapshot)
        return result, snapshot
    except Exception as e:
        print(f"[aggregator] shadow falhou (não-fatal): {type(e).__name__}: {e}")
        return None, None


def _run_rolling_context(
    conn,
    technical_result,
    ai_result,
    combined,
    gating_combined,
    trade_decision,
    gate_context,
    event_risk,
    risk_performance,
    gating_mode,
    aggregator_result,
):
    """Atualiza o Rolling Market Context (memória contextual do mercado).

    Sempre não-fatal. NÃO abre trades, NÃO bloqueia trades, NÃO altera gating.
    Activado por ROLLING_CONTEXT_ENABLED (default off).
    """
    if not _env_bool("ROLLING_CONTEXT_ENABLED", False):
        return None
    if not _env_bool("ROLLING_CONTEXT_UPDATE_EVERY_CYCLE", True):
        return None
    try:
        performance = context_snapshot.build_performance_snapshot(
            conn, PAIR, recent_performance=risk_performance
        )
        latest_prep = None
        try:
            latest_prep = database.get_latest_weekly_market_prep(conn, PAIR)
        except Exception:
            pass
        latest_rolling = None
        try:
            latest_rolling = database.get_latest_rolling_market_context(conn, PAIR)
        except Exception:
            pass
        snapshot = context_snapshot.build_market_snapshot(
            PAIR,
            technical_result,
            ai_result,
            combined,
            gating_combined,
            trade_decision,
            gate_context,
            event_risk,
            performance,
            gating_mode=gating_mode,
            latest_weekly_market_prep=latest_prep,
            latest_rolling_context=latest_rolling,
        )
        provider = (
            os.getenv("ROLLING_CONTEXT_PROVIDER")
            or os.getenv("AI_PROVIDER")
            or "groq"
        ).strip().lower()
        lookback_hours = _env_int("ROLLING_CONTEXT_LOOKBACK_HOURS", 24)
        max_prev_chars = _env_int("ROLLING_CONTEXT_MAX_PREVIOUS_SUMMARY_CHARS", 2500)
        result = rolling_context.update(
            conn,
            pair=PAIR,
            snapshot=snapshot,
            aggregator_result=aggregator_result,
            provider=provider,
            lookback_hours=lookback_hours,
            max_prev_chars=max_prev_chars,
        )
        return result
    except Exception as e:
        print(f"[rolling-context] ciclo falhou (não-fatal): {type(e).__name__}: {e}")
        return None


def _recent_risk_performance(conn, pair, limit=30):
    trades = database.get_paper_trades(conn, limit=limit, status=None, source="combined")
    closed = [
        trade for trade in trades
        if trade.get("pair") == pair and trade.get("status") in ("win", "loss")
    ]
    loss_streak = 0
    for trade in closed:
        if trade.get("status") == "loss":
            loss_streak += 1
            continue
        break
    metrics = database.calculate_analytics_metrics(conn, pair=pair, limit=limit)
    return {
        "loss_streak": loss_streak,
        "max_drawdown": metrics.get("max_drawdown"),
        "winrate": metrics.get("winrate"),
        "expectancy": metrics.get("expectancy"),
    }


def _print_signal_outcomes(outcomes):
    if not outcomes:
        return

    print()
    print("┌─── QUALIDADE DOS SINAIS (BACKFILL) ────────")

    label_map = {
        "shadow_technical": "shadow técnica",
        "shadow_combined": "shadow combinado",
        "combined": "combinado estrito",
    }

    has_data = False
    for source_key, label in label_map.items():
        horizons = outcomes.get(source_key, {})
        rows = []
        for horizon in ("1h", "4h", "24h"):
            stats = horizons.get(horizon) or {}
            count = stats.get("count", 0)
            if count == 0:
                continue
            rows.append(
                f"{horizon}: {stats['wins']}/{count} wins "
                f"({stats['win_rate']}%) avg {stats['avg_pips']} pips"
            )
        if rows:
            has_data = True
            print(f"│  {label}:")
            for row in rows:
                print(f"│    {row}")

    if not has_data:
        print("│  Sem outcomes ainda (aguarda candles futuros).")
    print("└────────────────────────────────────────────")


def _print_historical_summary(summary):
    print()
    print("┌─── HISTÓRICO RECENTE ──────────────────────")
    if not summary or summary.get("total", 0) == 0:
        print("│  Sem decisões guardadas ainda.")
        print("└────────────────────────────────────────────")
        return

    print(f"│  Últimas decisões:      {summary['total']}")
    print(f"│  Trades permitidos:     {summary['allowed']}")
    print(f"│  Trades bloqueados:     {summary['blocked']}")
    print(
        "│  BUY / SELL / NEUTRAL:  "
        f"{summary['buy']} / {summary['sell']} / {summary['neutral']}"
    )
    print(
        "│  Shadow BUY / SELL / NEUTRAL:  "
        f"{summary['shadow_buy']} / {summary['shadow_sell']} / {summary['shadow_neutral']}"
    )
    print(
        "│  Shadow combinado BUY/SELL/NEUTRAL:  "
        f"{summary.get('shadow_combined_buy', 0)} / "
        f"{summary.get('shadow_combined_sell', 0)} / "
        f"{summary.get('shadow_combined_neutral', 0)}"
    )
    print(f"│  Confiança média:       {summary['average_confidence']}%")
    print(f"│  Evento perigoso:       {summary['dangerous_event_count']} vezes")
    reason = summary["most_common_block_reason"] or "-"
    print(f"│  Motivo mais comum:     {reason}")
    print("└────────────────────────────────────────────")


def _run_weekend_mode(conn, cache_config, provider):
    """Executa o ciclo de fim-de-semana.

    Actualiza notícias e calendário, corre a preparação semanal se for o momento,
    exporta o dashboard. NÃO executa análise técnica, IA fundamental, nem paper trades.
    """
    wm_config = weekend_mode_config()
    wp_config = weekly_prep_config()

    print("=== Forex AI Bot — Weekend Mode ===")
    print(f"Provider: {provider}\n")

    relevant_news = []
    events = []

    if wm_config["update_news"]:
        print("[weekend] A actualizar notícias...")
        try:
            relevant_news, _, news_origin = _get_news(conn, cache_config)
            print(f"          {len(relevant_news)} artigos relevantes ({news_origin})")
        except Exception as e:
            print(f"          Falhou (não-fatal): {type(e).__name__}: {e}")

    if wm_config["update_calendar"]:
        print("[weekend] A actualizar calendário...")
        try:
            events, _, events_origin = _get_events(conn, cache_config)
            print(f"          {len(events)} eventos ({events_origin})")
        except Exception as e:
            print(f"          Falhou (não-fatal): {type(e).__name__}: {e}")

    if wp_config["enabled"] and is_weekly_prep_due(conn=conn, pair=PAIR):
        print("[weekend] A executar preparação semanal da semana...")
        try:
            prep = run_weekly_prep(conn, relevant_news, events, pair=PAIR, provider=provider)
            print(
                f"          bias={prep.get('macro_bias')} | "
                f"dir={prep.get('preferred_direction')} | "
                f"conf={prep.get('confidence')}% | "
                f"rec={prep.get('recommendation')} | "
                f"status={prep.get('status')}"
            )
        except Exception as e:
            print(f"          Falhou (não-fatal): {type(e).__name__}: {e}")
    else:
        print("[weekend] Preparação semanal: fora do horário ou já correu hoje.")

    if wm_config["export_logs"]:
        try:
            export_web_data()
            print("[weekend] Dashboard exportado.")
        except Exception as e:
            print(f"[weekend] Export falhou (não-fatal): {type(e).__name__}: {e}")

    print("\n[weekend] Ciclo fim-de-semana concluído. Nenhum trade foi aberto.")


def main():
    internet_ok, internet_status = _has_internet_connection()
    if not internet_ok:
        _print_no_internet(internet_status)
        return

    conn = database.connect()
    database.init_db(conn)
    cache_config = _load_cache_config()
    provider = (os.getenv("AI_PROVIDER") or "groq").strip().lower()

    wm_config = weekend_mode_config()
    if wm_config["enabled"] and is_weekend_mode_active():
        _run_weekend_mode(conn, cache_config, provider)
        conn.close()
        return

    print(f"=== Forex AI Bot — análise para {PAIR} ===")
    print(f"Provider activo: {provider}\n")
    print(f"Cache activo: {cache_config['use_cache']} | Force refresh: {cache_config['force_refresh']}\n")

    print("[1/4] A recolher notícias (RSS + scrape + APIs)...")
    relevant_news, news_sources, news_origin = _get_news(conn, cache_config)
    print(f"      {len(relevant_news)} artigos relevantes para {PAIR} ({news_origin})")

    print("[2/4] A ler calendário (RSS económico + FX Street)...")
    events, event_sources, events_origin = _get_events(conn, cache_config)
    print(f"      {len(events)} eventos únicos de alto impacto ({events_origin})")
    macro_result = get_macro_risk(PAIR, datetime.now(timezone.utc), events=events)

    print()
    _print_sources(news_sources, event_sources)

    print("\n[3/4] A correr análise técnica multi-timeframe...")
    technical_result, candles, candle_origins, timeframe_warnings = _get_multi_timeframe_technical(
        conn, cache_config, count=260
    )
    candles_origin = candle_origins.get("h1", "unknown")
    candle_counts = technical_result.get("timeframe_candle_counts") or {}
    print(
        "      candles: "
        + ", ".join(
            f"{tf.upper()}={candle_counts.get(tf, 0)} ({candle_origins.get(tf, 'unknown')})"
            for tf in multi_timeframe.ORDERED_TIMEFRAMES
        )
        + f"; H1 principal={len(candles)} ({candles_origin})"
    )
    for warning in timeframe_warnings:
        print(f"      aviso: {warning}")

    print("[4/4] A analisar com IA (com snapshot técnico)...")
    ai_result, input_hash, ai_origin = _get_ai_result(
        conn,
        cache_config,
        provider,
        relevant_news,
        events,
        technical=technical_result,
        macro_context_snapshot=macro_result["macro_context_snapshot"],
    )
    print(f"      análise IA: {ai_origin} ({input_hash[:12]})")

    print()
    scoring_config = scoring.load_combined_scoring_config()
    combined = _combine_signals(ai_result, technical_result, scoring_config=scoring_config)
    shadow_combined = _shadow_combine(ai_result, technical_result)
    current_price = technical_result.get("indicators", {}).get("current_price")
    atr_pips = technical_result.get("indicators", {}).get("atr_pips")

    # Computar scores antes do gating para podermos escolher o sinal efectivo.
    ai_score_value = _ai_context_score(ai_result)
    technical_score_value = technical_result.get("indicators", {}).get("technical_score")
    if technical_score_value is None:
        technical_score_value = scoring.technical_votes_score(
            technical_result.get("indicators", {}).get("rsi_vote",
                technical_result.get("indicators", {}).get("rsi_signal", "neutral")),
            technical_result.get("indicators", {}).get("ema_vote",
                technical_result.get("indicators", {}).get("ema_trend", "neutral")),
            technical_result.get("indicators", {}).get("macd_vote",
                technical_result.get("indicators", {}).get("macd_signal", "neutral")),
        )
    shadow_score_value = scoring.signal_score(
        technical_result.get("shadow_technical_signal"),
        technical_result.get("shadow_technical_confidence"),
    )
    ai_voted = not _ai_abstains(ai_result)
    combined_score_value = scoring.combine_scores(
        ai_score_value if ai_voted else None, technical_score_value,
        shadow_score=shadow_score_value,
        news_score=None,
        config=scoring_config,
    )
    score_signal_value = scoring.score_to_signal(combined_score_value, scoring_config)
    confidence_adjustment, confidence_adjustment_reasons = _decision_confidence_adjustment(technical_result)

    gating_mode = (os.getenv("GATING_MODE") or "score").strip().lower()
    gating_combined, gating_mode_used = _select_gating_signal(
        combined, score_signal_value, combined_score_value,
        shadow_combined, gating_mode,
    )

    event_risk = database.find_high_impact_event_nearby(
        conn,
        _env_int("EVENT_BLOCK_WINDOW_MINUTES", 120),
        relevant_currencies=_pair_currencies(PAIR),
    )

    # ── Macro Economic Calendar Filter ───────────────────────────────────────
    if macro_result["macro_block"]:
        print(
            f"[MACRO FILTER] Trade blocked\n"
            f"  Pair: {PAIR}\n"
            f"  Event: {macro_result['macro_event_title']}\n"
            f"  Currency: {macro_result['macro_event_currency']}\n"
            f"  Minutes distance: {macro_result['macro_minutes_distance']}\n"
            f"  Reason: {macro_result['macro_reason']}"
        )
    elif macro_result["macro_risk_level"] == "medium":
        factor = _env_float("MACRO_MEDIUM_IMPACT_CONFIDENCE_FACTOR", 0.8)
        original_conf = gating_combined.get("confidence", 0) / 100.0
        adjusted_conf = original_conf * factor
        gating_combined = dict(gating_combined)
        gating_combined["confidence"] = int(round(adjusted_conf * 100))
        print(
            f"[MACRO FILTER] Confidence reduced\n"
            f"  Pair: {PAIR}\n"
            f"  Event: {macro_result['macro_event_title']}\n"
            f"  Currency: {macro_result['macro_event_currency']}\n"
            f"  Original confidence: {original_conf:.2f}\n"
            f"  Adjusted confidence: {adjusted_conf:.2f}"
        )
    # ── End Macro Filter ─────────────────────────────────────────────────────

    market_state = forex_market_state()
    paper_source = "combined"
    op_state = operational_state(
        mode=os.getenv("BOT_MODE") or "trade",
        tolerance_minutes=_env_int("TRADE_WINDOW_TOLERANCE_MINUTES", 0),
    )
    cooldown = _cooldown_state(
        conn,
        PAIR,
        paper_source,
        gating_combined.get("signal"),
    )
    signal_persistence = _signal_persistence(conn, PAIR, gating_combined.get("signal"))
    risk_performance = _recent_risk_performance(conn, PAIR)
    gate_context = {
        "market": market_state,
        "cooldown": cooldown,
        "event": event_risk,
        "operational": op_state,
        "allow_buy": _env_bool("ALLOW_BUY", True),
        "allow_sell": _env_bool("ALLOW_SELL", True),
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
        "performance": risk_performance,
        "macro": macro_result,
    }
    trade_decision = evaluate_trade(
        PAIR,
        gating_combined,
        current_price,
        event_risk,
        atr_pips=atr_pips,
        technical_indicators=technical_result.get("indicators", {}),
        gate_context=gate_context,
    )

    # Apply macro block after evaluate_trade so all gate diagnostics are intact
    if macro_result["macro_block"]:
        trade_decision["trade_allowed"] = False
        trade_decision["simulated_order"] = None
        trade_decision["block_reason"] = "high_impact_macro_event"
        trade_decision.setdefault("gate_reasons", [])
        if "high_impact_macro_event" not in trade_decision["gate_reasons"]:
            trade_decision["gate_reasons"].append("high_impact_macro_event")

    aggregator_result, aggregator_snapshot = _run_aggregator_shadow(
        conn,
        technical_result,
        ai_result,
        combined,
        gating_combined,
        trade_decision,
        gate_context,
        event_risk,
        risk_performance,
        gating_mode_used,
    )

    rolling_context_result = _run_rolling_context(
        conn,
        technical_result,
        ai_result,
        combined,
        gating_combined,
        trade_decision,
        gate_context,
        event_risk,
        risk_performance,
        gating_mode_used,
        aggregator_result,
    )
    if rolling_context_result is not None:
        print(
            f"[rolling-context] phase={rolling_context_result.get('market_phase')} "
            f"bias={rolling_context_result.get('combined_bias')} "
            f"conf={rolling_context_result.get('confidence')}% "
            f"stance={rolling_context_result.get('recommended_stance')} "
            f"risk={rolling_context_result.get('risk_level')}"
        )

    _print_final(ai_result, technical_result, combined, trade_decision)
    print()
    if aggregator_result is not None:
        print(
            f"IA agregadora (shadow): {aggregator_result['ai_aggregated_signal']} "
            f"({aggregator_result['ai_aggregated_confidence']}%) "
            f"should_trade={aggregator_result['should_trade']} "
            f"risk={aggregator_result['risk_level']} "
            f"[{aggregator_result.get('status', 'ok')}]"
        )
    print(
        f"Shadow combined: {shadow_combined['signal']} "
        f"({shadow_combined['confidence']}%) — {shadow_combined['reason']}"
    )
    print(
        f"Score: AI={ai_score_value:+.2f}{'' if ai_voted else ' (abstém)'} "
        f"tech={technical_score_value:+.2f} "
        f"shadow={shadow_score_value:+.2f} combined={combined_score_value:+.2f} "
        f"-> {score_signal_value}"
    )
    _ai_conf_raw = ai_result.get("confidence") or 0
    _ai_adj = ai_result.get("confidence_adjustment") or 0.0
    _w = scoring_config
    _nr = combined.get("neutral_reason") or ""
    print(
        f"[scoring-pipeline] "
        f"ai_signal={ai_result.get('signal', 'NEUTRAL')} "
        f"conf_adj={float(_ai_adj):+.4f} "
        f"ai_conf={_ai_conf_raw} "
        f"ai_score={ai_score_value:+.4f}"
        f"{' (abstém — conf<' + str(int(_env_float('AI_VOTE_MIN_CONFIDENCE', 35.0))) + ')' if not ai_voted else ''} "
        f"tech_score={technical_score_value:+.4f} "
        f"pesos=[AI={_w['ai_weight']:.2f} tech={_w['technical_weight']:.2f} news={_w.get('news_weight', 0.0):.2f}] "
        f"combined={combined_score_value:+.4f} -> {score_signal_value}"
        + (f" neutral_reason={_nr}" if score_signal_value == "NEUTRAL" and _nr else "")
    )
    print(
        f"Multi-TF: M15={technical_result.get('technical_score_m15'):+.2f} "
        f"H1={technical_result.get('technical_score_h1'):+.2f} "
        f"H4={technical_result.get('technical_score_h4'):+.2f} "
        f"D1={technical_result.get('technical_score_d1'):+.2f} "
        f"-> {technical_result.get('multi_timeframe_score'):+.2f} "
        f"({technical_result.get('timeframe_alignment')})"
    )
    print(
        f"Gating mode: {gating_mode_used} -> {gating_combined['signal']} "
        f"({gating_combined['confidence']}%)"
    )
    print(
        f"Operacional: {op_state['mode']} can_trade={op_state['can_open_trade']} "
        f"{op_state.get('block_reason') or ''}"
    )

    source_status = {
        "news": news_origin,
        "calendar": events_origin,
        "ai": ai_origin,
        "candles": candles_origin,
    }
    signature = _decision_signature(technical_result, ai_result, combined, current_price)
    last_signature, last_timestamp = database.get_last_decision_signature(conn, PAIR)
    dedup_window = _env_int("DEDUP_WINDOW_MINUTES", 50)
    is_duplicate = _is_recent_duplicate(signature, last_signature, last_timestamp, dedup_window)
    decision_entry = _build_decision_entry(
        PAIR,
        TIMEFRAME,
        source_status,
        ai_result,
        technical_result,
        combined,
        shadow_combined,
        trade_decision,
        signature,
        candles,
        provider,
        scoring_config,
        ai_score=ai_score_value,
        technical_score=technical_score_value,
        shadow_score=shadow_score_value,
        combined_score=combined_score_value,
        score_combined_signal=score_signal_value,
        gating_mode=gating_mode_used,
        gating_signal=gating_combined.get("signal"),
        gating_confidence=gating_combined.get("confidence"),
        operational_state=op_state,
    )
    decision_entry["is_duplicate"] = is_duplicate
    if aggregator_result is not None:
        decision_entry["ai_aggregated"] = aggregator_result

    # Macro filter fields
    decision_entry["macro_risk_level"] = macro_result["macro_risk_level"]
    decision_entry["macro_block"] = macro_result["macro_block"]
    decision_entry["macro_event_title"] = macro_result["macro_event_title"]
    decision_entry["macro_event_currency"] = macro_result["macro_event_currency"]
    decision_entry["macro_event_time"] = macro_result["macro_event_time"]
    decision_entry["macro_minutes_distance"] = macro_result["macro_minutes_distance"]
    decision_entry["macro_reason"] = macro_result["macro_reason"]
    decision_entry["macro_context_snapshot"] = macro_result["macro_context_snapshot"]

    jsonl_saved, jsonl_error = _save_jsonl(decision_entry)
    sqlite_saved, sqlite_error, decision_id = _save_sqlite_decision(conn, decision_entry)
    if aggregator_result is not None and decision_id is not None:
        try:
            database.update_decision_aggregator(conn, decision_id, aggregator_result)
        except Exception as e:
            print(f"[aggregator] gravação shadow falhou (não-fatal): {type(e).__name__}: {e}")
    paper_trade_id = _maybe_create_paper_trade(
        conn,
        decision_id=decision_id,
        pair=PAIR,
        timeframe=TIMEFRAME,
        ai_result=ai_result,
        combined=gating_combined,
        current_price=current_price,
        atr_pips=atr_pips,
        created_at_dt=datetime.now(timezone.utc),
        trade_decision=trade_decision,
    )
    if paper_trade_id is not None:
        print(f"[paper-trade] criado #{paper_trade_id} ({decision_entry['ai_signal']}/"
              f"{decision_entry['combined_signal']})")

    try:
        analytics = database.save_analytics_metrics(conn, pair=PAIR)
        print(
            "[analytics] "
            f"winrate={analytics.get('winrate')} avg_rr={analytics.get('average_rr')} "
            f"pf={analytics.get('profit_factor')} expectancy={analytics.get('expectancy')}"
        )
    except Exception as e:
        print(f"[analytics] falhou: {type(e).__name__}: {e}")

    outcome_stats = database.update_decision_outcomes(
        conn,
        PAIR,
        TIMEFRAME,
        PRICE_PROVIDER,
    )

    try:
        export_web_data()
    except Exception as e:
        print(f"[web] export falhou: {type(e).__name__}: {e}")

    _print_run_summary(
        source_status,
        ai_result,
        technical_result,
        combined,
        trade_decision,
        jsonl_saved,
        sqlite_saved,
        is_duplicate,
        jsonl_error,
        sqlite_error,
    )
    if outcome_stats["cells_updated"] > 0:
        print(
            f"Outcomes preenchidos: {outcome_stats['rows_updated']} decisões "
            f"({outcome_stats['cells_updated']} colunas)"
        )

    print(f"\nLog JSONL: {DECISIONS_LOG}")
    print(f"SQLite DB: {database.DB_PATH}")
    _print_recent_decisions(database.get_recent_decisions(conn))
    _print_historical_summary(database.get_recent_decision_quality(conn, limit=20))
    _print_signal_outcomes(database.get_signal_outcomes(conn, PAIR, limit=200))
    conn.close()


if __name__ == "__main__":
    main()
