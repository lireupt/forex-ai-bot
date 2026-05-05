import json
import os
import hashlib
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

from modules.ai_analyst import analyse as analyse_ai, build_analysis_input
from modules import database
from modules.news_scraper import fetch_all_events, fetch_all_news
from modules.price_feed import PROVIDER as PRICE_PROVIDER, fetch_candles
from modules.risk import evaluate_trade
from modules.technical import analyse as analyse_technical
from scripts.export_logs import export as export_web_data

load_dotenv()

PAIR = "EUR/USD"
TIMEFRAME = "1h"
DECISIONS_LOG = Path("logs/decisions.jsonl")


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


def _get_candles(conn, cache_config, count=100):
    if cache_config["use_cache"] and not cache_config["force_refresh"]:
        cached = database.get_recent_market_candles(
            conn,
            PAIR,
            TIMEFRAME,
            PRICE_PROVIDER,
            _cutoff(minutes=cache_config["price_cache_minutes"]),
            count,
        )
        if cached:
            return _candles_to_df(cached), "cache"

    candles = fetch_candles(pair=PAIR, timeframe=TIMEFRAME, count=count)
    if not candles.empty:
        database.save_market_candles(
            conn,
            _df_to_candles(candles),
            PAIR,
            TIMEFRAME,
            PRICE_PROVIDER,
        )
    return candles, "fresh"


def _get_ai_result(conn, cache_config, provider, news, events):
    input_text = build_analysis_input(news, events, PAIR)
    input_hash = hashlib.sha256(input_text.encode("utf-8")).hexdigest()
    today = datetime.now(timezone.utc).date().isoformat()

    if cache_config["use_cache"] and cache_config["ai_cache_daily"] and not cache_config["force_refresh"]:
        cached = database.get_ai_analysis(conn, PAIR, today, input_hash, provider)
        if cached:
            return cached, input_hash, "cache"

    result = analyse_ai(news, events, PAIR)
    database.save_ai_analysis(conn, PAIR, today, input_hash, result)
    return result, input_hash, "fresh"


def _combine_signals(ai_result, technical_result):
    ai_signal = ai_result.get("signal", "NEUTRAL")
    technical_signal = technical_result.get("signal", "NEUTRAL")
    reasoning = ai_result.get("reasoning", "")

    if ai_signal == "NEUTRAL" or technical_signal == "NEUTRAL":
        return {
            "signal": "NEUTRAL",
            "confidence": 0,
            "hold_off": True,
            "reasoning": reasoning,
            "agreement": False,
        }

    if ai_signal == technical_signal:
        return {
            "signal": ai_signal,
            "confidence": round((ai_result.get("confidence", 0) + technical_result.get("confidence", 0)) / 2),
            "hold_off": bool(ai_result.get("hold_off", False)),
            "reasoning": reasoning,
            "agreement": True,
        }

    return {
        "signal": "NEUTRAL",
        "confidence": 0,
        "hold_off": True,
        "reasoning": f"{reasoning} [Discordância IA/técnica]",
        "agreement": False,
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
):
    indicators = technical_result.get("indicators", {})
    details = _technical_details(technical_result)
    order = trade_decision.get("simulated_order") or {}
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
        "stop_loss_pips_used": order.get("stop_loss_pips"),
        "take_profit_pips_used": order.get("take_profit_pips"),
        "sl_tp_mode": order.get("sl_tp_mode"),
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
        database.save_decision(conn, entry)
        return True, ""
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


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


def main():
    internet_ok, internet_status = _has_internet_connection()
    if not internet_ok:
        _print_no_internet(internet_status)
        return

    conn = database.connect()
    database.init_db(conn)
    cache_config = _load_cache_config()
    provider = (os.getenv("AI_PROVIDER") or "groq").strip().lower()
    print(f"=== Forex AI Bot — análise para {PAIR} ===")
    print(f"Provider activo: {provider}\n")
    print(f"Cache activo: {cache_config['use_cache']} | Force refresh: {cache_config['force_refresh']}\n")

    print("[1/4] A recolher notícias (RSS + scrape + APIs)...")
    relevant_news, news_sources, news_origin = _get_news(conn, cache_config)
    print(f"      {len(relevant_news)} artigos relevantes para {PAIR} ({news_origin})")

    print("[2/4] A ler calendário (RSS económico + FX Street)...")
    events, event_sources, events_origin = _get_events(conn, cache_config)
    print(f"      {len(events)} eventos únicos de alto impacto ({events_origin})")

    print()
    _print_sources(news_sources, event_sources)

    print("\n[3/4] A analisar com IA...")
    ai_result, input_hash, ai_origin = _get_ai_result(conn, cache_config, provider, relevant_news, events)
    print(f"      análise IA: {ai_origin} ({input_hash[:12]})")

    print("[4/4] A correr análise técnica...")
    candles, candles_origin = _get_candles(conn, cache_config, count=100)
    technical_result = analyse_technical(candles, PAIR)
    print(f"      {len(candles)} candles lidas ({candles_origin})")

    print()
    combined = _combine_signals(ai_result, technical_result)
    shadow_combined = _shadow_combine(ai_result, technical_result)
    current_price = technical_result.get("indicators", {}).get("current_price")
    atr_pips = technical_result.get("indicators", {}).get("atr_pips")
    event_risk = database.find_high_impact_event_nearby(
        conn,
        _env_int("EVENT_BLOCK_WINDOW_MINUTES", 120),
        relevant_currencies=_pair_currencies(PAIR),
    )
    trade_decision = evaluate_trade(
        PAIR,
        combined,
        current_price,
        event_risk,
        atr_pips=atr_pips,
    )
    _print_final(ai_result, technical_result, combined, trade_decision)
    print()
    print(
        f"Shadow combined: {shadow_combined['signal']} "
        f"({shadow_combined['confidence']}%) — {shadow_combined['reason']}"
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
    )

    if is_duplicate:
        jsonl_saved, jsonl_error = False, f"duplicado de {last_signature}"
        sqlite_saved, sqlite_error = False, f"duplicado de {last_signature}"
    else:
        jsonl_saved, jsonl_error = _save_jsonl(decision_entry)
        sqlite_saved, sqlite_error = _save_sqlite_decision(conn, decision_entry)

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
