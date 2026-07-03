import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from modules import analytics_metrics
from modules import decision_engine
from modules.event_rules import parse_event_time as _parse_event_time

PIP_SIZE = 0.0001

DB_PATH = Path("data/forex_bot.db")


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS market_candles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pair TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            candle_time TEXT NOT NULL,
            open REAL NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            close REAL NOT NULL,
            volume REAL NOT NULL,
            provider TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(pair, timeframe, candle_time, provider)
        );

        CREATE TABLE IF NOT EXISTS news_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            summary TEXT,
            url TEXT NOT NULL,
            source TEXT,
            published_at TEXT,
            pair TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(url)
        );

        CREATE TABLE IF NOT EXISTS economic_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            country TEXT,
            impact TEXT,
            event_time TEXT NOT NULL,
            source TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(title, event_time, source)
        );

        CREATE TABLE IF NOT EXISTS ai_analyses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pair TEXT NOT NULL,
            analysis_date TEXT NOT NULL,
            input_hash TEXT NOT NULL,
            signal TEXT NOT NULL,
            confidence INTEGER NOT NULL,
            reasoning TEXT,
            risk_level TEXT,
            hold_off INTEGER NOT NULL,
            provider TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(pair, analysis_date, input_hash, provider)
        );

        CREATE TABLE IF NOT EXISTS decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            pair TEXT NOT NULL,
            timeframe TEXT,
            news_source_status TEXT,
            calendar_source_status TEXT,
            ai_source_status TEXT,
            candles_source_status TEXT,
            rsi_vote TEXT,
            ema_vote TEXT,
            macd_vote TEXT,
            rsi_value REAL,
            ema20_value REAL,
            ema50_value REAL,
            macd_value REAL,
            macd_signal_value REAL,
            atr14_value REAL,
            atr_price REAL,
            atr_pips REAL,
            volatility_reason TEXT,
            technical_reason TEXT,
            shadow_technical_signal TEXT,
            shadow_technical_confidence INTEGER,
            shadow_technical_reason TEXT,
            technical_signal TEXT,
            ai_signal TEXT,
            combined_signal TEXT,
            confidence INTEGER,
            hold_off INTEGER,
            current_price REAL,
            trade_allowed INTEGER,
            block_reason TEXT,
            dangerous_event_nearby INTEGER,
            dangerous_event_reason TEXT,
            simulated_order_json TEXT,
            decision_hash TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS gate_checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            checked_at TEXT NOT NULL,
            status TEXT NOT NULL,
            total_trades INTEGER,
            wins INTEGER,
            losses INTEGER,
            expired INTEGER,
            win_rate REAL,
            profit_factor REAL,
            avg_r REAL,
            max_streak_losses INTEGER,
            max_drawdown_pct REAL,
            details_json TEXT NOT NULL,
            config_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS analytics_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            calculated_at TEXT NOT NULL,
            pair TEXT,
            winrate REAL,
            average_rr REAL,
            profit_factor REAL,
            expectancy REAL,
            max_drawdown REAL,
            sharpe_ratio REAL,
            average_score REAL,
            ai_impact REAL,
            h4_d1_impact REAL,
            alignment_success_rate REAL,
            metrics_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS paper_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            decision_id INTEGER,
            pair TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            direction TEXT NOT NULL,
            entry_price REAL NOT NULL,
            simulated_sl REAL NOT NULL,
            simulated_tp REAL NOT NULL,
            sl_pips REAL,
            tp_pips REAL,
            atr_pips REAL,
            atr_price REAL,
            status TEXT NOT NULL DEFAULT 'open',
            source TEXT,
            signal_source TEXT,
            created_at TEXT NOT NULL,
            expiry_at TEXT,
            close_price REAL,
            closed_at TEXT,
            close_reason TEXT,
            result_pips REAL,
            result_r_multiple REAL
        );

        CREATE TABLE IF NOT EXISTS weekly_market_prep (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            pair TEXT NOT NULL,
            week_start TEXT,
            macro_bias TEXT,
            preferred_direction TEXT,
            confidence INTEGER,
            risk_level TEXT,
            recommendation TEXT,
            summary TEXT,
            reasoning_summary TEXT,
            key_weekend_news_json TEXT,
            key_events_next_week_json TEXT,
            market_opening_risks_json TEXT,
            warnings_json TEXT,
            raw_response_json TEXT,
            provider TEXT,
            model_version TEXT,
            status TEXT
        );

        CREATE TABLE IF NOT EXISTS rolling_market_context (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            pair TEXT NOT NULL,
            previous_context_id INTEGER,
            market_phase TEXT,
            macro_bias TEXT,
            technical_bias TEXT,
            combined_bias TEXT,
            confidence INTEGER,
            risk_level TEXT,
            short_summary TEXT,
            what_changed TEXT,
            persistent_factors_json TEXT,
            new_factors_json TEXT,
            invalidated_factors_json TEXT,
            key_risks_json TEXT,
            likely_market_intent TEXT,
            recommended_stance TEXT,
            should_trade_bias INTEGER,
            should_reduce_risk INTEGER,
            raw_response_json TEXT
        );
        """
    )
    _ensure_ai_analysis_columns(conn)
    _ensure_decisions_columns(conn)
    _ensure_paper_trades_columns(conn)
    conn.commit()


def _ensure_ai_analysis_columns(conn):
    existing = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(ai_analyses)").fetchall()
    }
    columns = {
        "bias": "TEXT",
        "confidence_adjustment": "REAL",
        "risk_adjustment": "REAL",
        "macro_context": "TEXT",
        "volatility_context": "TEXT",
        "news_sentiment": "TEXT",
        "context_reason": "TEXT",
    }
    for name, column_type in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE ai_analyses ADD COLUMN {name} {column_type}")


def _ensure_decisions_columns(conn):
    existing = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(decisions)").fetchall()
    }
    columns = {
        "timeframe": "TEXT",
        "news_source_status": "TEXT",
        "calendar_source_status": "TEXT",
        "ai_source_status": "TEXT",
        "candles_source_status": "TEXT",
        "dangerous_event_nearby": "INTEGER",
        "dangerous_event_reason": "TEXT",
        "rsi_vote": "TEXT",
        "ema_vote": "TEXT",
        "macd_vote": "TEXT",
        "rsi_value": "REAL",
        "ema20_value": "REAL",
        "ema50_value": "REAL",
        "macd_value": "REAL",
        "macd_signal_value": "REAL",
        "atr14_value": "REAL",
        "atr_price": "REAL",
        "atr_pips": "REAL",
        "volatility_reason": "TEXT",
        "technical_reason": "TEXT",
        "shadow_technical_signal": "TEXT",
        "shadow_technical_confidence": "INTEGER",
        "shadow_technical_reason": "TEXT",
        "shadow_combined_signal": "TEXT",
        "shadow_combined_confidence": "INTEGER",
        "shadow_combined_reason": "TEXT",
        "decision_signature": "TEXT",
        "decision_hash": "TEXT",
        "is_duplicate": "INTEGER",
        "outcome_price_1h": "REAL",
        "outcome_price_4h": "REAL",
        "outcome_price_24h": "REAL",
        "outcome_updated_at": "TEXT",
        "stop_loss_pips_used": "REAL",
        "take_profit_pips_used": "REAL",
        "sl_tp_mode": "TEXT",
        "ai_score": "REAL",
        "ai_confidence_score": "REAL",
        "ai_analysis_text": "TEXT",
        "ai_reason": "TEXT",
        "ai_features_snapshot": "TEXT",
        "ai_model_version": "TEXT",
        "ai_bias": "TEXT",
        "ai_confidence_adjustment": "REAL",
        "ai_risk_adjustment": "REAL",
        "macro_context": "TEXT",
        "volatility_context": "TEXT",
        "news_sentiment": "TEXT",
        "ai_context_reason": "TEXT",
        "technical_score": "REAL",
        "technical_score_m15": "REAL",
        "technical_score_h1": "REAL",
        "technical_score_h4": "REAL",
        "technical_score_d1": "REAL",
        "multi_timeframe_score": "REAL",
        "timeframe_alignment": "TEXT",
        "timeframe_block_reason": "TEXT",
        "shadow_score": "REAL",
        "combined_score": "REAL",
        "combined_reason": "TEXT",
        "blocking_reason": "TEXT",
        "score_combined_signal": "TEXT",
        "paper_trade_id": "INTEGER",
        "gating_mode": "TEXT",
        "gating_signal": "TEXT",
        "gating_confidence": "INTEGER",
        "adx_value": "REAL",
        "gate_diagnostics_json": "TEXT",
        "ai_status": "TEXT",
        "neutral_reason": "TEXT",
        "operational_mode": "TEXT",
        "operational_can_trade": "INTEGER",
        "operational_block_reason": "TEXT",
        "ai_aggregated_signal": "TEXT",
        "ai_aggregated_confidence": "INTEGER",
        "ai_aggregated_score": "REAL",
        "ai_aggregated_risk_level": "TEXT",
        "ai_aggregated_should_trade": "INTEGER",
        "ai_aggregated_should_reduce_risk": "INTEGER",
        "ai_aggregated_reasoning": "TEXT",
        "ai_aggregated_supporting_factors": "TEXT",
        "ai_aggregated_contradicting_factors": "TEXT",
        "ai_aggregated_warnings": "TEXT",
        "ai_aggregated_status": "TEXT",
        "ai_aggregated_model_version": "TEXT",
        # Macro Economic Calendar Filter fields
        "macro_risk_level": "TEXT",
        "macro_block": "INTEGER",
        "macro_event_title": "TEXT",
        "macro_event_currency": "TEXT",
        "macro_event_time": "TEXT",
        "macro_minutes_distance": "REAL",
        "macro_reason": "TEXT",
        "macro_context_snapshot_json": "TEXT",
        # Camada de scoring de notícias fundamentado
        "news_score": "REAL",
        "news_score_basis": "TEXT",
        "num_articles": "INTEGER",
    }
    for name, column_type in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE decisions ADD COLUMN {name} {column_type}")


def _ensure_paper_trades_columns(conn):
    existing = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(paper_trades)").fetchall()
    }
    columns = {
        "decision_id": "INTEGER",
        "pair": "TEXT",
        "timeframe": "TEXT",
        "direction": "TEXT",
        "entry_price": "REAL",
        "simulated_sl": "REAL",
        "simulated_tp": "REAL",
        "sl_pips": "REAL",
        "tp_pips": "REAL",
        "atr_pips": "REAL",
        "atr_price": "REAL",
        "status": "TEXT",
        "source": "TEXT",
        "signal_source": "TEXT",
        "created_at": "TEXT",
        "expiry_at": "TEXT",
        "close_price": "REAL",
        "closed_at": "TEXT",
        "close_reason": "TEXT",
        "result_pips": "REAL",
        "result_r_multiple": "REAL",
        "current_price": "REAL",
        "last_price_checked_at": "TEXT",
        "distance_to_tp_pips": "REAL",
        "distance_to_sl_pips": "REAL",
    }
    for name, column_type in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE paper_trades ADD COLUMN {name} {column_type}")


def _rows_to_dicts(rows):
    return [dict(row) for row in rows]


def save_news_items(conn, items, pair):
    now = utc_now()
    for item in items:
        url = item.get("link") or item.get("url") or item.get("title", "")
        conn.execute(
            """
            INSERT OR IGNORE INTO news_items
            (title, summary, url, source, published_at, pair, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item.get("title", ""),
                item.get("summary", ""),
                url,
                item.get("source", ""),
                item.get("published", "") or item.get("published_at", ""),
                pair,
                now,
            ),
        )
    conn.commit()


def get_recent_news(conn, pair, since_iso):
    rows = conn.execute(
        """
        SELECT title, summary, url AS link, source, published_at AS published
        FROM news_items
        WHERE pair = ? AND created_at >= ?
        ORDER BY id ASC
        """,
        (pair, since_iso),
    ).fetchall()
    return _rows_to_dicts(rows)


def save_economic_events(conn, events):
    now = utc_now()
    for event in events:
        conn.execute(
            """
            INSERT OR IGNORE INTO economic_events
            (title, country, impact, event_time, source, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                event.get("event", ""),
                event.get("currency", ""),
                event.get("impact", ""),
                event.get("time", ""),
                event.get("source", "scraper"),
                now,
            ),
        )
    conn.commit()


def get_recent_events(conn, since_iso):
    rows = conn.execute(
        """
        SELECT country AS currency, title AS event, event_time AS time, impact
        FROM economic_events
        WHERE created_at >= ?
        ORDER BY id ASC
        """,
        (since_iso,),
    ).fetchall()
    return _rows_to_dicts(rows)


def _env_bool(name, default):
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name, default):
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    try:
        return int(float(value))
    except ValueError:
        return default


def get_high_impact_events(conn):
    """Todos os eventos de alto impacto guardados em `economic_events` —
    usado tanto por `find_high_impact_event_nearby` (live) como para
    alimentar `modules.decision_engine.MarketContext.high_impact_events`
    (backtest), garantindo que ambos partem do mesmo universo de dados."""
    raw_rows = conn.execute(
        """
        SELECT title, country, impact, event_time, source
        FROM economic_events
        WHERE lower(impact) = 'high'
        ORDER BY event_time ASC
        """
    ).fetchall()
    return [dict(row) for row in raw_rows]


def find_high_impact_event_nearby(conn, window_minutes, relevant_currencies=None):
    enabled = _env_bool("EVENT_FILTER_ENABLED", True)
    rows = get_high_impact_events(conn) if enabled else []

    return decision_engine.resolve_event_gate(
        rows,
        datetime.now(timezone.utc),
        window_minutes,
        relevant_currencies=relevant_currencies,
        enabled=enabled,
    )


def save_market_candles(conn, candles, pair, timeframe, provider):
    now = utc_now()
    for candle in candles:
        conn.execute(
            """
            INSERT OR REPLACE INTO market_candles
            (pair, timeframe, candle_time, open, high, low, close, volume, provider, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                pair,
                timeframe,
                candle["candle_time"],
                float(candle["open"]),
                float(candle["high"]),
                float(candle["low"]),
                float(candle["close"]),
                float(candle["volume"]),
                provider,
                now,
            ),
        )
    conn.commit()


def get_recent_market_candles(conn, pair, timeframe, provider, since_iso, count):
    rows = conn.execute(
        """
        SELECT candle_time, open, high, low, close, volume
        FROM market_candles
        WHERE pair = ? AND timeframe = ? AND provider = ? AND created_at >= ?
        ORDER BY candle_time DESC
        LIMIT ?
        """,
        (pair, timeframe, provider, since_iso, count),
    ).fetchall()
    candles = _rows_to_dicts(rows)
    candles.reverse()
    return candles


def get_ai_analysis(conn, pair, analysis_date, input_hash, provider):
    row = conn.execute(
        """
        SELECT signal, confidence, reasoning, risk_level, hold_off, provider,
               bias, confidence_adjustment, risk_adjustment, macro_context,
               volatility_context, news_sentiment, context_reason
        FROM ai_analyses
        WHERE pair = ? AND analysis_date = ? AND input_hash = ? AND provider = ?
        """,
        (pair, analysis_date, input_hash, provider),
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["hold_off"] = bool(result["hold_off"])
    result["bias"] = result.get("bias") or result.get("signal") or "NEUTRAL"
    result["confidence_adjustment"] = float(result.get("confidence_adjustment") or 0.0)
    result["risk_adjustment"] = float(result.get("risk_adjustment") or 0.0)
    result["macro_context"] = result.get("macro_context") or "cached_legacy"
    result["volatility_context"] = result.get("volatility_context") or "medium"
    result["news_sentiment"] = result.get("news_sentiment") or "neutral"
    result["reason"] = result.get("context_reason") or result.get("reasoning") or ""
    return result


def save_ai_analysis(conn, pair, analysis_date, input_hash, result):
    if result.get("status") == "failed":
        return
    conn.execute(
        """
        INSERT OR IGNORE INTO ai_analyses
        (pair, analysis_date, input_hash, signal, confidence, reasoning, risk_level,
         hold_off, provider, bias, confidence_adjustment, risk_adjustment,
         macro_context, volatility_context, news_sentiment, context_reason, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            pair,
            analysis_date,
            input_hash,
            result.get("signal", "NEUTRAL"),
            int(result.get("confidence", 0)),
            result.get("reasoning", ""),
            result.get("risk_level", ""),
            int(bool(result.get("hold_off", True))),
            result.get("provider", ""),
            result.get("bias", result.get("signal", "NEUTRAL")),
            float(result.get("confidence_adjustment") or 0.0),
            float(result.get("risk_adjustment") or 0.0),
            result.get("macro_context", ""),
            result.get("volatility_context", ""),
            result.get("news_sentiment", ""),
            result.get("reason", result.get("reasoning", "")),
            utc_now(),
        ),
    )
    conn.commit()


def save_decision(conn, entry):
    decision_hash = entry.get("decision_hash") or entry.get("decision_signature")
    features_snapshot = entry.get("ai_features_snapshot")
    if isinstance(features_snapshot, (dict, list)):
        features_snapshot_json = json.dumps(features_snapshot, ensure_ascii=False)
    else:
        features_snapshot_json = features_snapshot

    gate_diagnostics = entry.get("gate_diagnostics")
    if isinstance(gate_diagnostics, (dict, list)):
        gate_diagnostics_json = json.dumps(gate_diagnostics, ensure_ascii=False)
    else:
        gate_diagnostics_json = gate_diagnostics

    cursor = conn.execute(
        """
        INSERT INTO decisions
        (timestamp, pair, timeframe, news_source_status, calendar_source_status,
         ai_source_status, candles_source_status, rsi_vote, ema_vote, macd_vote,
         rsi_value, ema20_value, ema50_value, macd_value, macd_signal_value,
         atr14_value, atr_price, atr_pips, volatility_reason, technical_reason,
         shadow_technical_signal, shadow_technical_confidence, shadow_technical_reason,
         shadow_combined_signal, shadow_combined_confidence, shadow_combined_reason,
         technical_signal, ai_signal, combined_signal, confidence, hold_off,
         current_price, trade_allowed, block_reason, dangerous_event_nearby,
         dangerous_event_reason, simulated_order_json, decision_signature,
         decision_hash, is_duplicate,
         stop_loss_pips_used, take_profit_pips_used, sl_tp_mode,
         ai_score, ai_confidence_score, ai_analysis_text, ai_reason, ai_features_snapshot,
         ai_model_version, ai_bias, ai_confidence_adjustment, ai_risk_adjustment,
         macro_context, volatility_context, news_sentiment, ai_context_reason,
         technical_score, technical_score_m15, technical_score_h1,
         technical_score_h4, technical_score_d1, multi_timeframe_score,
         timeframe_alignment, timeframe_block_reason, shadow_score, combined_score,
         combined_reason, blocking_reason, score_combined_signal,
         gating_mode, gating_signal, gating_confidence, adx_value,
         gate_diagnostics_json, ai_status, neutral_reason, operational_mode,
         operational_can_trade, operational_block_reason,
         macro_risk_level, macro_block, macro_event_title, macro_event_currency,
         macro_event_time, macro_minutes_distance, macro_reason,
         macro_context_snapshot_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            entry["timestamp"],
            entry["pair"],
            entry.get("timeframe"),
            entry.get("news_source_status"),
            entry.get("calendar_source_status"),
            entry.get("ai_source_status"),
            entry.get("candles_source_status"),
            entry.get("rsi_vote"),
            entry.get("ema_vote"),
            entry.get("macd_vote"),
            entry.get("rsi_value"),
            entry.get("ema20_value"),
            entry.get("ema50_value"),
            entry.get("macd_value"),
            entry.get("macd_signal_value"),
            entry.get("atr14_value"),
            entry.get("atr_price"),
            entry.get("atr_pips"),
            entry.get("volatility_reason"),
            entry.get("technical_reason"),
            entry.get("shadow_technical_signal"),
            entry.get("shadow_technical_confidence"),
            entry.get("shadow_technical_reason"),
            entry.get("shadow_combined_signal"),
            entry.get("shadow_combined_confidence"),
            entry.get("shadow_combined_reason"),
            entry.get("technical_signal"),
            entry.get("ai_signal"),
            entry.get("combined_signal"),
            entry.get("confidence"),
            int(bool(entry.get("hold_off"))),
            entry.get("current_price"),
            int(bool(entry.get("trade_allowed"))),
            entry.get("block_reason"),
            int(bool(entry.get("dangerous_event_nearby"))),
            entry.get("dangerous_event_reason"),
            json.dumps(entry.get("simulated_order"), ensure_ascii=False)
            if entry.get("simulated_order") is not None
            else None,
            entry.get("decision_signature"),
            decision_hash,
            int(bool(entry.get("is_duplicate"))),
            entry.get("stop_loss_pips_used"),
            entry.get("take_profit_pips_used"),
            entry.get("sl_tp_mode"),
            entry.get("ai_score"),
            entry.get("ai_confidence_score"),
            entry.get("ai_analysis_text"),
            entry.get("ai_reason"),
            features_snapshot_json,
            entry.get("ai_model_version"),
            entry.get("ai_bias"),
            entry.get("ai_confidence_adjustment"),
            entry.get("ai_risk_adjustment"),
            entry.get("macro_context"),
            entry.get("volatility_context"),
            entry.get("news_sentiment"),
            entry.get("ai_context_reason"),
            entry.get("technical_score"),
            entry.get("technical_score_m15"),
            entry.get("technical_score_h1"),
            entry.get("technical_score_h4"),
            entry.get("technical_score_d1"),
            entry.get("multi_timeframe_score"),
            entry.get("timeframe_alignment"),
            entry.get("timeframe_block_reason"),
            entry.get("shadow_score"),
            entry.get("combined_score"),
            entry.get("combined_reason"),
            entry.get("blocking_reason"),
            entry.get("score_combined_signal"),
            entry.get("gating_mode"),
            entry.get("gating_signal"),
            entry.get("gating_confidence"),
            entry.get("adx_value"),
            gate_diagnostics_json,
            entry.get("ai_status"),
            entry.get("neutral_reason"),
            entry.get("operational_mode"),
            int(bool(entry.get("operational_can_trade"))),
            entry.get("operational_block_reason"),
            entry.get("macro_risk_level"),
            int(bool(entry.get("macro_block"))) if entry.get("macro_block") is not None else 0,
            entry.get("macro_event_title"),
            entry.get("macro_event_currency"),
            entry.get("macro_event_time"),
            entry.get("macro_minutes_distance"),
            entry.get("macro_reason"),
            json.dumps(entry.get("macro_context_snapshot"), ensure_ascii=False)
            if entry.get("macro_context_snapshot") is not None
            else None,
            utc_now(),
        ),
    )
    conn.commit()
    return cursor.lastrowid


def update_decision_aggregator(conn, decision_id, result):
    """Grava o parecer da IA agregadora (shadow) numa decisão já persistida.

    Update dedicado para não tocar no INSERT posicional de `save_decision`.
    Listas são serializadas em JSON. Não levanta se `decision_id` for None.
    """
    if not decision_id or not result:
        return False

    def _json(value):
        if isinstance(value, (list, dict)):
            return json.dumps(value, ensure_ascii=False)
        return value

    conn.execute(
        """
        UPDATE decisions
        SET ai_aggregated_signal = ?,
            ai_aggregated_confidence = ?,
            ai_aggregated_score = ?,
            ai_aggregated_risk_level = ?,
            ai_aggregated_should_trade = ?,
            ai_aggregated_should_reduce_risk = ?,
            ai_aggregated_reasoning = ?,
            ai_aggregated_supporting_factors = ?,
            ai_aggregated_contradicting_factors = ?,
            ai_aggregated_warnings = ?,
            ai_aggregated_status = ?,
            ai_aggregated_model_version = ?
        WHERE id = ?
        """,
        (
            result.get("ai_aggregated_signal"),
            result.get("ai_aggregated_confidence"),
            result.get("ai_aggregated_score"),
            result.get("risk_level"),
            int(bool(result.get("should_trade"))),
            int(bool(result.get("should_reduce_risk"))),
            result.get("reasoning_summary"),
            _json(result.get("supporting_factors")),
            _json(result.get("contradicting_factors")),
            _json(result.get("warnings")),
            result.get("status", "ok"),
            result.get("model_version"),
            decision_id,
        ),
    )
    conn.commit()
    return True


def update_decision_news_score(conn, decision_id, news_score_value, news_score_basis, num_articles):
    """Grava o score de notícias numa decisão já persistida (non-fatal, não bloqueia).

    Update dedicado para não tocar no INSERT posicional de `save_decision`.
    Segue o mesmo padrão de `update_decision_aggregator`.
    """
    if not decision_id:
        return False
    conn.execute(
        """
        UPDATE decisions
        SET news_score = ?,
            news_score_basis = ?,
            num_articles = ?
        WHERE id = ?
        """,
        (
            round(float(news_score_value), 4) if news_score_value is not None else None,
            news_score_basis,
            int(num_articles) if num_articles is not None else None,
            decision_id,
        ),
    )
    conn.commit()
    return True


def get_recent_paper_trades_for_direction(conn, pair, source, direction, since_iso):
    rows = conn.execute(
        """
        SELECT *
        FROM paper_trades
        WHERE pair = ? AND source = ? AND direction = ? AND created_at >= ?
        ORDER BY created_at DESC
        """,
        (pair, source, direction, since_iso),
    ).fetchall()
    return _rows_to_dicts(rows)


def get_recent_paper_trades_since(conn, pair, source, since_iso):
    """Como `get_recent_paper_trades_for_direction` mas sem filtrar por
    direcção — usado para alimentar `modules.decision_engine.MarketContext`
    com histórico suficiente para recalcular cooldown por qualquer direcção
    de forma pura."""
    rows = conn.execute(
        """
        SELECT *
        FROM paper_trades
        WHERE pair = ? AND source = ? AND created_at >= ?
        ORDER BY created_at DESC
        """,
        (pair, source, since_iso),
    ).fetchall()
    return _rows_to_dicts(rows)


def get_last_closed_paper_trade(conn, pair, source=None):
    clauses = ["pair = ?", "status in ('win', 'loss')", "closed_at IS NOT NULL"]
    params = [pair]
    if source:
        clauses.append("source = ?")
        params.append(source)
    row = conn.execute(
        f"""
        SELECT *
        FROM paper_trades
        WHERE {' AND '.join(clauses)}
        ORDER BY closed_at DESC
        LIMIT 1
        """,
        tuple(params),
    ).fetchone()
    return dict(row) if row is not None else None


def link_decision_to_paper_trade(conn, decision_id, paper_trade_id):
    if decision_id is None or paper_trade_id is None:
        return
    conn.execute(
        "UPDATE decisions SET paper_trade_id = ? WHERE id = ?",
        (paper_trade_id, decision_id),
    )
    conn.commit()


def create_paper_trade(conn, paper_trade):
    cursor = conn.execute(
        """
        INSERT INTO paper_trades
        (decision_id, pair, timeframe, direction, entry_price, simulated_sl,
         simulated_tp, sl_pips, tp_pips, atr_pips, atr_price, status, source,
         signal_source, created_at, expiry_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            paper_trade.get("decision_id"),
            paper_trade["pair"],
            paper_trade["timeframe"],
            paper_trade["direction"],
            paper_trade["entry_price"],
            paper_trade["simulated_sl"],
            paper_trade["simulated_tp"],
            paper_trade.get("sl_pips"),
            paper_trade.get("tp_pips"),
            paper_trade.get("atr_pips"),
            paper_trade.get("atr_price"),
            paper_trade.get("status", "open"),
            paper_trade.get("source"),
            paper_trade.get("signal_source"),
            paper_trade.get("created_at", utc_now()),
            paper_trade.get("expiry_at"),
        ),
    )
    conn.commit()
    return cursor.lastrowid


def update_paper_trade_monitor_price(
    conn,
    paper_trade_id,
    current_price,
    last_price_checked_at,
    distance_to_tp_pips,
    distance_to_sl_pips,
):
    conn.execute(
        """
        UPDATE paper_trades
        SET current_price = ?,
            last_price_checked_at = ?,
            distance_to_tp_pips = ?,
            distance_to_sl_pips = ?
        WHERE id = ? AND status = 'open'
        """,
        (
            current_price,
            last_price_checked_at,
            distance_to_tp_pips,
            distance_to_sl_pips,
            paper_trade_id,
        ),
    )
    conn.commit()


def get_open_paper_trades(conn, pair=None):
    if pair:
        rows = conn.execute(
            "SELECT * FROM paper_trades WHERE status = 'open' AND pair = ? ORDER BY id ASC",
            (pair,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM paper_trades WHERE status = 'open' ORDER BY id ASC"
        ).fetchall()
    return _rows_to_dicts(rows)


def update_paper_trade_result(
    conn,
    paper_trade_id,
    status,
    close_price,
    closed_at,
    close_reason,
    result_pips,
    result_r_multiple,
):
    conn.execute(
        """
        UPDATE paper_trades
        SET status = ?, close_price = ?, closed_at = ?, close_reason = ?,
            result_pips = ?, result_r_multiple = ?
        WHERE id = ?
        """,
        (
            status,
            close_price,
            closed_at,
            close_reason,
            result_pips,
            result_r_multiple,
            paper_trade_id,
        ),
    )
    conn.commit()


def get_market_candles_between(conn, pair, timeframe, start_iso, end_iso, provider=None):
    if provider:
        rows = conn.execute(
            """
            SELECT candle_time, open, high, low, close, volume
            FROM market_candles
            WHERE pair = ? AND timeframe = ? AND provider = ?
              AND candle_time >= ? AND candle_time <= ?
            ORDER BY candle_time ASC
            """,
            (pair, timeframe, provider, start_iso, end_iso),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT candle_time, open, high, low, close, volume
            FROM market_candles
            WHERE pair = ? AND timeframe = ?
              AND candle_time >= ? AND candle_time <= ?
            ORDER BY candle_time ASC
            """,
            (pair, timeframe, start_iso, end_iso),
        ).fetchall()
    return _rows_to_dicts(rows)


def get_paper_trades(conn, limit=200, status=None, source=None):
    clauses = []
    params = []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if source:
        clauses.append("source = ?")
        params.append(source)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT * FROM paper_trades
        {where}
        ORDER BY id DESC
        LIMIT ?
        """,
        tuple(params),
    ).fetchall()
    return _rows_to_dicts(rows)


def save_gate_check(conn, snapshot):
    overall = snapshot.get("overall", {})
    metrics = overall.get("metrics", {})
    config = snapshot.get("config", {})
    cursor = conn.execute(
        """
        INSERT INTO gate_checks
        (checked_at, status, total_trades, wins, losses, expired,
         win_rate, profit_factor, avg_r, max_streak_losses,
         max_drawdown_pct, details_json, config_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            snapshot.get("checked_at", utc_now()),
            overall.get("status", "partial"),
            (metrics.get("wins", 0) or 0)
            + (metrics.get("losses", 0) or 0)
            + (metrics.get("expired", 0) or 0),
            metrics.get("wins"),
            metrics.get("losses"),
            metrics.get("expired"),
            metrics.get("win_rate"),
            metrics.get("profit_factor"),
            metrics.get("avg_r"),
            metrics.get("max_losing_streak"),
            metrics.get("max_drawdown_pct"),
            json.dumps(snapshot, ensure_ascii=False),
            json.dumps(config, ensure_ascii=False),
        ),
    )
    conn.commit()
    return cursor.lastrowid


def get_recent_gate_checks(conn, limit=20):
    rows = conn.execute(
        """
        SELECT id, checked_at, status, total_trades, wins, losses, expired,
               win_rate, profit_factor, avg_r, max_streak_losses, max_drawdown_pct
        FROM gate_checks
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return _rows_to_dicts(rows)


def get_latest_gate_check(conn):
    row = conn.execute(
        """
        SELECT details_json
        FROM gate_checks
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row["details_json"])
    except (json.JSONDecodeError, TypeError):
        return None


def get_paper_trades_summary(conn, source=None, direction=None):
    clauses = []
    params = []
    if source:
        clauses.append("source = ?")
        params.append(source)
    if direction:
        clauses.append("direction = ?")
        params.append(direction)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM paper_trades {where}",
        tuple(params),
    ).fetchall()
    trades = _rows_to_dicts(rows)
    total = len(trades)
    wins = sum(1 for t in trades if t.get("status") == "win")
    losses = sum(1 for t in trades if t.get("status") == "loss")
    expired = sum(1 for t in trades if t.get("status") == "expired")
    open_count = sum(1 for t in trades if t.get("status") == "open")
    closed = wins + losses
    pips_values = [t.get("result_pips") for t in trades if t.get("result_pips") is not None]
    r_values = [t.get("result_r_multiple") for t in trades if t.get("result_r_multiple") is not None]
    avg_pips = round(sum(pips_values) / len(pips_values), 1) if pips_values else None
    avg_r = round(sum(r_values) / len(r_values), 2) if r_values else None
    win_rate = round(wins / closed * 100, 1) if closed else None
    best = max(pips_values) if pips_values else None
    worst = min(pips_values) if pips_values else None
    return {
        "total": total,
        "open": open_count,
        "wins": wins,
        "losses": losses,
        "expired": expired,
        "win_rate": win_rate,
        "avg_pips": avg_pips,
        "avg_r": avg_r,
        "best_pips": round(best, 1) if best is not None else None,
        "worst_pips": round(worst, 1) if worst is not None else None,
    }


def get_last_decision_signature(conn, pair):
    row = conn.execute(
        """
        SELECT COALESCE(decision_signature, decision_hash) AS decision_signature, timestamp
        FROM decisions
        WHERE pair = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (pair,),
    ).fetchone()
    if row is None:
        return None, None
    return row["decision_signature"], row["timestamp"]


def _candle_close_at(conn, pair, timeframe, provider, target_dt, tolerance_hours=2):
    earliest = (target_dt - timedelta(hours=tolerance_hours)).isoformat()
    latest = (target_dt + timedelta(hours=tolerance_hours)).isoformat()
    row = conn.execute(
        """
        SELECT close
        FROM market_candles
        WHERE pair = ? AND timeframe = ? AND provider = ?
          AND candle_time BETWEEN ? AND ?
        ORDER BY abs(julianday(candle_time) - julianday(?)) ASC
        LIMIT 1
        """,
        (pair, timeframe, provider, earliest, latest, target_dt.isoformat()),
    ).fetchone()
    if row is None:
        return None
    return float(row["close"])


def update_decision_outcomes(conn, pair, timeframe, provider, max_rows=200):
    rows = conn.execute(
        """
        SELECT id, timestamp, outcome_price_1h, outcome_price_4h, outcome_price_24h
        FROM decisions
        WHERE pair = ?
          AND (outcome_price_1h IS NULL
               OR outcome_price_4h IS NULL
               OR outcome_price_24h IS NULL)
        ORDER BY id ASC
        LIMIT ?
        """,
        (pair, max_rows),
    ).fetchall()

    deltas = (
        (1, "outcome_price_1h"),
        (4, "outcome_price_4h"),
        (24, "outcome_price_24h"),
    )

    updated_rows = 0
    updated_cells = 0
    now = datetime.now(timezone.utc)

    for row in rows:
        ts = _parse_event_time(row["timestamp"])
        if ts is None:
            continue

        row_changed = False
        for hours, column in deltas:
            if row[column] is not None:
                continue
            target = ts + timedelta(hours=hours)
            if target > now:
                continue
            price = _candle_close_at(conn, pair, timeframe, provider, target)
            if price is None:
                continue
            conn.execute(
                f"UPDATE decisions SET {column} = ?, outcome_updated_at = ? WHERE id = ?",
                (price, utc_now(), row["id"]),
            )
            updated_cells += 1
            row_changed = True

        if row_changed:
            updated_rows += 1

    conn.commit()
    return {"rows_updated": updated_rows, "cells_updated": updated_cells}


def _signed_pips(signal, entry_price, future_price):
    if signal not in ("BUY", "SELL"):
        return None
    if entry_price is None or future_price is None:
        return None
    delta = future_price - entry_price
    if signal == "SELL":
        delta = -delta
    return round(delta / PIP_SIZE, 1)


def _accumulate_outcomes(buckets, signal, entry_price, future_price):
    pips = _signed_pips(signal, entry_price, future_price)
    if pips is None:
        return
    buckets["count"] += 1
    buckets["pips_sum"] += pips
    if pips > 0:
        buckets["wins"] += 1
    elif pips < 0:
        buckets["losses"] += 1


def _empty_bucket():
    return {"count": 0, "wins": 0, "losses": 0, "pips_sum": 0.0}


def _summarise_bucket(bucket):
    count = bucket["count"]
    if count == 0:
        return {
            "count": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": None,
            "avg_pips": None,
        }
    return {
        "count": count,
        "wins": bucket["wins"],
        "losses": bucket["losses"],
        "win_rate": round(bucket["wins"] / count * 100, 1),
        "avg_pips": round(bucket["pips_sum"] / count, 1),
    }


def get_signal_outcomes(conn, pair, limit=200):
    rows = conn.execute(
        """
        SELECT current_price, shadow_technical_signal, shadow_combined_signal,
               combined_signal,
               outcome_price_1h, outcome_price_4h, outcome_price_24h
        FROM decisions
        WHERE pair = ?
          AND (outcome_price_1h IS NOT NULL
               OR outcome_price_4h IS NOT NULL
               OR outcome_price_24h IS NOT NULL)
        ORDER BY id DESC
        LIMIT ?
        """,
        (pair, limit),
    ).fetchall()

    horizons = ("1h", "4h", "24h")
    sources = ("shadow_technical", "shadow_combined", "combined")
    buckets = {src: {h: _empty_bucket() for h in horizons} for src in sources}

    outcome_columns = {
        "1h": "outcome_price_1h",
        "4h": "outcome_price_4h",
        "24h": "outcome_price_24h",
    }
    signal_columns = {
        "shadow_technical": "shadow_technical_signal",
        "shadow_combined": "shadow_combined_signal",
        "combined": "combined_signal",
    }

    for row in rows:
        entry = row["current_price"]
        for source, sig_col in signal_columns.items():
            signal = row[sig_col]
            for horizon, price_col in outcome_columns.items():
                _accumulate_outcomes(
                    buckets[source][horizon],
                    signal,
                    entry,
                    row[price_col],
                )

    return {
        source: {h: _summarise_bucket(buckets[source][h]) for h in horizons}
        for source in sources
    }


def get_recent_decisions(conn, limit=5):
    rows = conn.execute(
        """
        SELECT timestamp, pair, combined_signal, confidence, trade_allowed, block_reason
        FROM decisions
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return _rows_to_dicts(rows)


def get_recent_gating_decisions(conn, pair, limit=5):
    rows = conn.execute(
        """
        SELECT timestamp, pair, combined_signal, gating_signal, gating_confidence,
               trade_allowed, block_reason
        FROM decisions
        WHERE pair = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (pair, limit),
    ).fetchall()
    return _rows_to_dicts(rows)


def get_recent_decisions_for_context(conn, pair, limit=30):
    """Colunas necessárias para `modules.decision_engine` recalcular, de
    forma pura, tanto o signal persistence (as `limit` mais recentes, dos
    quais só as primeiras N contam) como as métricas de performance
    (`modules.analytics_metrics`) — uma única query cobre ambos os usos que
    hoje são duas queries separadas (`get_recent_gating_decisions` e a
    query de `decisions` dentro de `calculate_analytics_metrics`)."""
    rows = conn.execute(
        """
        SELECT timestamp, pair, combined_signal, gating_signal, gating_confidence,
               trade_allowed, block_reason, combined_score, ai_score,
               multi_timeframe_score, technical_score_h4, technical_score_d1,
               timeframe_alignment
        FROM decisions
        WHERE pair = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (pair, limit),
    ).fetchall()
    return _rows_to_dicts(rows)


def get_recent_decision_quality(conn, limit=20):
    rows = conn.execute(
        """
        SELECT combined_signal, confidence, trade_allowed, block_reason,
               dangerous_event_nearby, shadow_technical_signal,
               shadow_combined_signal
        FROM decisions
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    decisions = _rows_to_dicts(rows)
    total = len(decisions)
    if total == 0:
        return {"total": 0}

    allowed = sum(1 for row in decisions if row["trade_allowed"])
    blocked = total - allowed
    signal_counts = {"BUY": 0, "SELL": 0, "NEUTRAL": 0}
    shadow_counts = {"BUY": 0, "SELL": 0, "NEUTRAL": 0}
    shadow_combined_counts = {"BUY": 0, "SELL": 0, "NEUTRAL": 0}
    confidence_total = 0
    event_count = 0
    block_reasons = {}

    for row in decisions:
        signal = row["combined_signal"] or "NEUTRAL"
        if signal not in signal_counts:
            signal = "NEUTRAL"
        signal_counts[signal] += 1
        shadow_signal = row["shadow_technical_signal"] or "NEUTRAL"
        if shadow_signal not in shadow_counts:
            shadow_signal = "NEUTRAL"
        shadow_counts[shadow_signal] += 1
        shadow_combined = row["shadow_combined_signal"] or "NEUTRAL"
        if shadow_combined not in shadow_combined_counts:
            shadow_combined = "NEUTRAL"
        shadow_combined_counts[shadow_combined] += 1
        confidence_total += row["confidence"] or 0
        if row["dangerous_event_nearby"]:
            event_count += 1
        reason = row["block_reason"]
        if reason:
            block_reasons[reason] = block_reasons.get(reason, 0) + 1

    most_common_reason = ""
    if block_reasons:
        most_common_reason = max(block_reasons.items(), key=lambda item: item[1])[0]

    return {
        "total": total,
        "allowed": allowed,
        "blocked": blocked,
        "buy": signal_counts["BUY"],
        "sell": signal_counts["SELL"],
        "neutral": signal_counts["NEUTRAL"],
        "shadow_buy": shadow_counts["BUY"],
        "shadow_sell": shadow_counts["SELL"],
        "shadow_neutral": shadow_counts["NEUTRAL"],
        "shadow_combined_buy": shadow_combined_counts["BUY"],
        "shadow_combined_sell": shadow_combined_counts["SELL"],
        "shadow_combined_neutral": shadow_combined_counts["NEUTRAL"],
        "average_confidence": round(confidence_total / total),
        "dangerous_event_count": event_count,
        "most_common_block_reason": most_common_reason,
    }


def get_calibration_summary(conn, since_iso=None, pair=None):
    decision_params = []
    decision_where = []
    if since_iso:
        decision_where.append("timestamp >= ?")
        decision_params.append(since_iso)
    if pair:
        decision_where.append("pair = ?")
        decision_params.append(pair)
    decision_clause = "WHERE " + " AND ".join(decision_where) if decision_where else ""
    decision_rows = conn.execute(
        f"""
        SELECT id, timestamp, pair, trade_allowed, block_reason, blocking_reason,
               confidence, gating_confidence, gating_signal, combined_signal
        FROM decisions
        {decision_clause}
        ORDER BY id ASC
        """,
        tuple(decision_params),
    ).fetchall()
    decisions = _rows_to_dicts(decision_rows)

    total_decisions = len(decisions)
    total_executed = sum(1 for row in decisions if row.get("trade_allowed"))
    total_blocked = total_decisions - total_executed
    blocked_by_reason = {}
    executed_confidences = []
    all_confidences = []

    for row in decisions:
        confidence = row.get("gating_confidence")
        if confidence is None:
            confidence = row.get("confidence")
        if confidence is not None:
            all_confidences.append(float(confidence))
        if row.get("trade_allowed") and confidence is not None:
            executed_confidences.append(float(confidence))
        if not row.get("trade_allowed"):
            reason = row.get("blocking_reason") or row.get("block_reason") or "unknown"
            blocked_by_reason[reason] = blocked_by_reason.get(reason, 0) + 1

    trade_params = []
    trade_where = []
    if since_iso:
        trade_where.append("created_at >= ?")
        trade_params.append(since_iso)
    if pair:
        trade_where.append("pair = ?")
        trade_params.append(pair)
    trade_clause = "WHERE " + " AND ".join(trade_where) if trade_where else ""
    trade_rows = conn.execute(
        f"""
        SELECT direction, status, result_pips, result_r_multiple
        FROM paper_trades
        {trade_clause}
        ORDER BY id ASC
        """,
        tuple(trade_params),
    ).fetchall()
    trades = _rows_to_dicts(trade_rows)
    wins = sum(1 for trade in trades if trade.get("status") == "win")
    losses = sum(1 for trade in trades if trade.get("status") == "loss")
    closed = wins + losses
    pips = [
        float(trade["result_pips"])
        for trade in trades
        if trade.get("result_pips") is not None
    ]
    gross_profit = sum(value for value in pips if value > 0)
    gross_loss = abs(sum(value for value in pips if value < 0))
    buy_pips = [
        float(trade["result_pips"])
        for trade in trades
        if trade.get("direction") == "BUY" and trade.get("result_pips") is not None
    ]
    sell_pips = [
        float(trade["result_pips"])
        for trade in trades
        if trade.get("direction") == "SELL" and trade.get("result_pips") is not None
    ]
    buy_total = sum(1 for trade in trades if trade.get("direction") == "BUY")
    sell_total = sum(1 for trade in trades if trade.get("direction") == "SELL")
    buy_net = round(sum(buy_pips), 1) if buy_pips else 0.0
    sell_net = round(sum(sell_pips), 1) if sell_pips else 0.0
    if buy_net == sell_net:
        best_direction = None
    else:
        best_direction = "BUY" if buy_net > sell_net else "SELL"

    return {
        "total_decisions": total_decisions,
        "total_blocked": total_blocked,
        "total_executed": total_executed,
        "block_rate": round(total_blocked / total_decisions * 100, 1) if total_decisions else None,
        "blocked_by_reason": dict(sorted(blocked_by_reason.items(), key=lambda item: (-item[1], item[0]))),
        "top_block_reason": max(blocked_by_reason.items(), key=lambda item: item[1])[0] if blocked_by_reason else None,
        "wins": wins,
        "losses": losses,
        "winrate": round(wins / closed * 100, 1) if closed else None,
        "avg_confidence": round(sum(executed_confidences) / len(executed_confidences), 1)
        if executed_confidences
        else (round(sum(all_confidences) / len(all_confidences), 1) if all_confidences else None),
        "avg_pips": round(sum(pips) / len(pips), 1) if pips else None,
        "buy_vs_sell": {"BUY": buy_total, "SELL": sell_total},
        "net_pips": round(sum(pips), 1) if pips else 0.0,
        "profit_factor": round(gross_profit / gross_loss, 2)
        if gross_loss
        else (999.0 if gross_profit > 0 else None),
        "expectancy": round(sum(pips) / closed, 1) if closed and pips else None,
        "best_direction": best_direction,
        "direction_net_pips": {"BUY": buy_net, "SELL": sell_net},
    }


def _aggregator_trade_metrics(trades):
    """Métricas de um conjunto de paper trades (mesma convenção do resto do projeto).

    winrate e expectancy contam apenas trades decisivos (win/loss); expired entra
    em `trades`/`net_pips` mas não no denominador de winrate.
    """
    total = len(trades)
    wins = sum(1 for t in trades if t.get("status") == "win")
    losses = sum(1 for t in trades if t.get("status") == "loss")
    expired = sum(1 for t in trades if t.get("status") == "expired")
    closed = wins + losses
    pips = [float(t["result_pips"]) for t in trades if t.get("result_pips") is not None]
    gross_profit = sum(value for value in pips if value > 0)
    gross_loss = abs(sum(value for value in pips if value < 0))
    return {
        "trades": total,
        "wins": wins,
        "losses": losses,
        "expired": expired,
        "winrate": round(wins / closed * 100, 1) if closed else None,
        "avg_pips": round(sum(pips) / len(pips), 1) if pips else None,
        "net_pips": round(sum(pips), 1) if pips else 0.0,
        "profit_factor": round(gross_profit / gross_loss, 2)
        if gross_loss
        else (999.0 if gross_profit > 0 else None),
        "expectancy": round(sum(pips) / closed, 2) if closed and pips else None,
    }


def _delta(after, before):
    if after is None or before is None:
        return None
    return round(after - before, 2)


def get_aggregator_analysis(conn, since_iso=None, pair=None):
    """Análise estatística do voto da IA agregadora (shadow) — só medição.

    Cruza os paper trades fechados com o veredicto da IA gravado na decisão
    associada. NÃO altera nenhuma decisão, gate ou resultado. Resiliente a bases
    de dados antigas sem as colunas `ai_aggregated_*`.
    """
    empty = {
        "available": False,
        "reason": "sem dados da IA agregadora",
        "window": since_iso or "all",
        "total_evaluated": 0,
        "should_trade_true": _aggregator_trade_metrics([]),
        "should_trade_false": _aggregator_trade_metrics([]),
        "baseline": _aggregator_trade_metrics([]),
        "impact_if_veto_enabled": {
            "winrate_change": None,
            "net_pips_change": None,
            "expectancy_change": None,
            "profit_factor_change": None,
        },
        "agreement": {
            "agree": 0,
            "disagree": 0,
            "agreement_rate": None,
            "winrate_when_agree": None,
            "winrate_when_disagree": None,
        },
        "risk_level": {
            level: {"trades": 0, "wins": 0, "losses": 0, "net_pips": 0.0}
            for level in ("low", "medium", "high")
        },
        "warnings": {},
        "recommendation": "Continuar em shadow mode",
        "recommendation_reasons": ["sem dados suficientes da IA agregadora"],
    }

    existing = {row["name"] for row in conn.execute("PRAGMA table_info(decisions)").fetchall()}
    if "ai_aggregated_should_trade" not in existing:
        empty["reason"] = "colunas ai_aggregated_* ausentes (base de dados antiga)"
        return empty

    where = ["d.ai_aggregated_should_trade IS NOT NULL", "pt.status IN ('win','loss','expired')"]
    params = []
    if since_iso:
        where.append("pt.created_at >= ?")
        params.append(since_iso)
    if pair:
        where.append("pt.pair = ?")
        params.append(pair)
    clause = "WHERE " + " AND ".join(where)
    rows = _rows_to_dicts(conn.execute(
        f"""
        SELECT pt.status AS status, pt.result_pips AS result_pips, pt.direction AS direction,
               d.ai_aggregated_signal AS ai_aggregated_signal,
               d.ai_aggregated_should_trade AS should_trade,
               d.ai_aggregated_risk_level AS risk_level,
               d.ai_aggregated_warnings AS warnings,
               d.technical_signal AS technical_signal
        FROM paper_trades pt
        JOIN decisions d ON pt.decision_id = d.id
        {clause}
        ORDER BY pt.id ASC
        """,
        tuple(params),
    ).fetchall())

    if not rows:
        empty["reason"] = "ainda não há paper trades fechados com veredicto da IA agregadora"
        return empty

    true_trades = [r for r in rows if r.get("should_trade") == 1]
    false_trades = [r for r in rows if r.get("should_trade") == 0]

    true_metrics = _aggregator_trade_metrics(true_trades)
    false_metrics = _aggregator_trade_metrics(false_trades)
    baseline = _aggregator_trade_metrics(rows)

    impact = {
        "winrate_change": _delta(true_metrics["winrate"], baseline["winrate"]),
        "net_pips_change": _delta(true_metrics["net_pips"], baseline["net_pips"]),
        "expectancy_change": _delta(true_metrics["expectancy"], baseline["expectancy"]),
        "profit_factor_change": _delta(true_metrics["profit_factor"], baseline["profit_factor"]),
    }

    agree_trades = []
    disagree_trades = []
    for row in rows:
        ai_sig = (row.get("ai_aggregated_signal") or "").upper()
        tech_sig = (row.get("technical_signal") or "").upper()
        if ai_sig and ai_sig == tech_sig:
            agree_trades.append(row)
        else:
            disagree_trades.append(row)
    agree_metrics = _aggregator_trade_metrics(agree_trades)
    disagree_metrics = _aggregator_trade_metrics(disagree_trades)
    agreement_total = len(agree_trades) + len(disagree_trades)
    agreement = {
        "agree": len(agree_trades),
        "disagree": len(disagree_trades),
        "agreement_rate": round(len(agree_trades) / agreement_total * 100, 1) if agreement_total else None,
        "winrate_when_agree": agree_metrics["winrate"],
        "winrate_when_disagree": disagree_metrics["winrate"],
    }

    risk_level = {}
    for level in ("low", "medium", "high"):
        bucket = [r for r in rows if (r.get("risk_level") or "").lower() == level]
        metrics = _aggregator_trade_metrics(bucket)
        risk_level[level] = {
            "trades": metrics["trades"],
            "wins": metrics["wins"],
            "losses": metrics["losses"],
            "net_pips": metrics["net_pips"],
        }

    warnings = {}
    for row in rows:
        raw = row.get("warnings")
        parsed = []
        if isinstance(raw, str) and raw.strip():
            try:
                loaded = json.loads(raw)
                if isinstance(loaded, list):
                    parsed = loaded
            except json.JSONDecodeError:
                parsed = []
        elif isinstance(raw, list):
            parsed = raw
        for warning in parsed:
            key = str(warning).strip()
            if key:
                warnings[key] = warnings.get(key, 0) + 1
    warnings = dict(sorted(warnings.items(), key=lambda item: (-item[1], item[0])))

    min_trades = _env_int("AGGREGATOR_ADVISORY_MIN_TRADES", 30)
    min_per_group = _env_int("AGGREGATOR_ADVISORY_MIN_PER_GROUP", 10)
    reasons = []
    ready = True
    if len(rows) < min_trades:
        ready = False
        reasons.append(f"amostra insuficiente ({len(rows)} < {min_trades} trades)")
    if len(true_trades) < min_per_group or len(false_trades) < min_per_group:
        ready = False
        reasons.append(
            f"grupos pequenos (should_trade=True={len(true_trades)}, "
            f"should_trade=False={len(false_trades)}; mínimo {min_per_group})"
        )
    if true_metrics["winrate"] is None or false_metrics["winrate"] is None:
        ready = False
        reasons.append("winrate indisponível num dos grupos")
    elif true_metrics["winrate"] <= false_metrics["winrate"]:
        ready = False
        reasons.append("winrate de should_trade=True não supera should_trade=False")
    if (impact["net_pips_change"] or 0) <= 0:
        ready = False
        reasons.append("vetar should_trade=False não melhora net_pips")
    if (impact["expectancy_change"] or 0) <= 0:
        ready = False
        reasons.append("vetar should_trade=False não melhora expectancy")

    if ready:
        recommendation = "IA pronta para modo advisory"
        reasons = [
            "amostra suficiente e separação consistente entre should_trade True/False",
            f"vetar should_trade=False melhoraria net_pips em {impact['net_pips_change']:+}",
        ]
    else:
        recommendation = "Continuar em shadow mode"

    return {
        "available": True,
        "reason": "",
        "window": since_iso or "all",
        "total_evaluated": len(rows),
        "should_trade_true": true_metrics,
        "should_trade_false": false_metrics,
        "baseline": baseline,
        "impact_if_veto_enabled": impact,
        "agreement": agreement,
        "risk_level": risk_level,
        "warnings": warnings,
        "recommendation": recommendation,
        "recommendation_reasons": reasons,
    }


def calculate_analytics_metrics(conn, pair=None, limit=500):
    params = []
    where = "WHERE status in ('win', 'loss')"
    if pair:
        where += " AND pair = ?"
        params.append(pair)
    rows = conn.execute(
        f"""
        SELECT result_pips, result_r_multiple
        FROM paper_trades
        {where}
        ORDER BY id DESC
        LIMIT ?
        """,
        tuple(params + [limit]),
    ).fetchall()
    trades = [dict(row) for row in rows]

    score_params = []
    score_where = ""
    if pair:
        score_where = "WHERE pair = ?"
        score_params.append(pair)
    decision_rows = conn.execute(
        f"""
        SELECT combined_score, ai_score, multi_timeframe_score,
               technical_score_h4, technical_score_d1, timeframe_alignment,
               trade_allowed
        FROM decisions
        {score_where}
        ORDER BY id DESC
        LIMIT ?
        """,
        tuple(score_params + [limit]),
    ).fetchall()
    decisions = [dict(row) for row in decision_rows]

    return analytics_metrics.compute_metrics(trades, decisions)


def save_analytics_metrics(conn, pair=None, metrics=None):
    metrics = metrics or calculate_analytics_metrics(conn, pair=pair)
    conn.execute(
        """
        INSERT INTO analytics_metrics
        (calculated_at, pair, winrate, average_rr, profit_factor, expectancy,
         max_drawdown, sharpe_ratio, average_score, ai_impact, h4_d1_impact,
         alignment_success_rate, metrics_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            utc_now(),
            pair,
            metrics.get("winrate"),
            metrics.get("average_rr"),
            metrics.get("profit_factor"),
            metrics.get("expectancy"),
            metrics.get("max_drawdown"),
            metrics.get("sharpe_ratio"),
            metrics.get("average_score"),
            metrics.get("ai_impact"),
            metrics.get("h4_d1_impact"),
            metrics.get("alignment_success_rate"),
            json.dumps(metrics, ensure_ascii=False),
        ),
    )
    conn.commit()
    return metrics


def save_weekly_market_prep(conn, result):
    """Grava o resultado da preparação semanal na tabela weekly_market_prep."""
    def _jdump(value):
        if isinstance(value, (list, dict)):
            return json.dumps(value, ensure_ascii=False)
        return value or "[]"

    raw = dict(result)
    cursor = conn.execute(
        """
        INSERT INTO weekly_market_prep
        (created_at, pair, week_start, macro_bias, preferred_direction,
         confidence, risk_level, recommendation, summary, reasoning_summary,
         key_weekend_news_json, key_events_next_week_json,
         market_opening_risks_json, warnings_json, raw_response_json,
         provider, model_version, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            utc_now(),
            result.get("pair", ""),
            result.get("week_start"),
            result.get("macro_bias"),
            result.get("preferred_direction"),
            result.get("confidence"),
            result.get("risk_level"),
            result.get("recommendation"),
            result.get("summary"),
            result.get("reasoning_summary"),
            _jdump(result.get("key_weekend_news")),
            _jdump(result.get("key_events_next_week")),
            _jdump(result.get("market_opening_risks")),
            _jdump(result.get("warnings")),
            json.dumps(raw, ensure_ascii=False),
            result.get("provider"),
            result.get("model_version"),
            result.get("status", "ok"),
        ),
    )
    conn.commit()
    return cursor.lastrowid


def get_latest_weekly_market_prep(conn, pair="EUR/USD"):
    """Devolve o registo mais recente de weekly_market_prep para o par, ou None."""
    try:
        row = conn.execute(
            """
            SELECT id, created_at, pair, week_start, macro_bias, preferred_direction,
                   confidence, risk_level, recommendation, summary, reasoning_summary,
                   key_weekend_news_json, key_events_next_week_json,
                   market_opening_risks_json, warnings_json, provider, model_version, status
            FROM weekly_market_prep
            WHERE pair = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (pair,),
        ).fetchone()
    except Exception:
        return None

    if row is None:
        return None

    record = dict(row)

    def _jload(value, default):
        if not value:
            return default
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return default

    record["key_weekend_news"] = _jload(record.pop("key_weekend_news_json", None), [])
    record["key_events_next_week"] = _jload(record.pop("key_events_next_week_json", None), [])
    record["market_opening_risks"] = _jload(record.pop("market_opening_risks_json", None), [])
    record["warnings"] = _jload(record.pop("warnings_json", None), [])
    return record


# ---------------------------------------------------------------------------
# Rolling Market Context helpers
# ---------------------------------------------------------------------------

def save_rolling_market_context(conn, pair, data, previous_context_id=None, raw_response=None):
    """Persiste um novo rolling market context. Devolve o id inserido."""
    now = utc_now()

    def _jdump(value):
        if value is None:
            return None
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False)

    conn.execute(
        """
        INSERT INTO rolling_market_context
        (created_at, pair, previous_context_id,
         market_phase, macro_bias, technical_bias, combined_bias,
         confidence, risk_level, short_summary, what_changed,
         persistent_factors_json, new_factors_json, invalidated_factors_json,
         key_risks_json, likely_market_intent, recommended_stance,
         should_trade_bias, should_reduce_risk, raw_response_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            now,
            pair,
            previous_context_id,
            data.get("market_phase"),
            data.get("macro_bias"),
            data.get("technical_bias"),
            data.get("combined_bias"),
            data.get("confidence"),
            data.get("risk_level"),
            data.get("short_summary"),
            data.get("what_changed"),
            _jdump(data.get("persistent_factors")),
            _jdump(data.get("new_factors")),
            _jdump(data.get("invalidated_factors")),
            _jdump(data.get("key_risks")),
            data.get("likely_market_intent"),
            data.get("recommended_stance"),
            1 if data.get("should_trade_bias") else 0,
            1 if data.get("should_reduce_risk") else 0,
            _jdump(raw_response),
        ),
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def _decode_rolling_context_row(row):
    """Converte uma row da tabela rolling_market_context em dict legível."""
    if row is None:
        return None
    record = dict(row)

    def _jload(value, default):
        if not value:
            return default
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return default

    record["persistent_factors"] = _jload(record.pop("persistent_factors_json", None), [])
    record["new_factors"] = _jload(record.pop("new_factors_json", None), [])
    record["invalidated_factors"] = _jload(record.pop("invalidated_factors_json", None), [])
    record["key_risks"] = _jload(record.pop("key_risks_json", None), [])
    record["should_trade_bias"] = bool(record.get("should_trade_bias"))
    record["should_reduce_risk"] = bool(record.get("should_reduce_risk"))
    return record


def get_latest_rolling_market_context(conn, pair):
    """Devolve o contexto mais recente para o par, ou None se não existir."""
    try:
        row = conn.execute(
            """
            SELECT id, created_at, pair, previous_context_id,
                   market_phase, macro_bias, technical_bias, combined_bias,
                   confidence, risk_level, short_summary, what_changed,
                   persistent_factors_json, new_factors_json, invalidated_factors_json,
                   key_risks_json, likely_market_intent, recommended_stance,
                   should_trade_bias, should_reduce_risk
            FROM rolling_market_context
            WHERE pair = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (pair,),
        ).fetchone()
    except Exception:
        return None
    return _decode_rolling_context_row(row)


def get_recent_rolling_market_context(conn, pair, limit=24):
    """Devolve os contextos mais recentes para o par (mais antigo primeiro)."""
    try:
        rows = conn.execute(
            """
            SELECT id, created_at, pair, previous_context_id,
                   market_phase, macro_bias, technical_bias, combined_bias,
                   confidence, risk_level, short_summary, what_changed,
                   persistent_factors_json, new_factors_json, invalidated_factors_json,
                   key_risks_json, likely_market_intent, recommended_stance,
                   should_trade_bias, should_reduce_risk
            FROM rolling_market_context
            WHERE pair = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (pair, limit),
        ).fetchall()
    except Exception:
        return []
    result = [_decode_rolling_context_row(r) for r in rows]
    result.reverse()
    return result
