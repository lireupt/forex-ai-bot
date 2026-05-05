import json
import sqlite3
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

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
            created_at TEXT NOT NULL
        );
        """
    )
    _ensure_decisions_columns(conn)
    conn.commit()


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
        "outcome_price_1h": "REAL",
        "outcome_price_4h": "REAL",
        "outcome_price_24h": "REAL",
        "outcome_updated_at": "TEXT",
        "stop_loss_pips_used": "REAL",
        "take_profit_pips_used": "REAL",
        "sl_tp_mode": "TEXT",
    }
    for name, column_type in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE decisions ADD COLUMN {name} {column_type}")


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


def _parse_event_time(value):
    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError, OverflowError):
            return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def find_high_impact_event_nearby(conn, window_minutes, relevant_currencies=None):
    now = datetime.now(timezone.utc)
    rows = conn.execute(
        """
        SELECT title, country, impact, event_time, source
        FROM economic_events
        WHERE lower(impact) = 'high'
        ORDER BY event_time ASC
        """
    ).fetchall()

    relevant = None
    if relevant_currencies:
        relevant = {c.strip().upper() for c in relevant_currencies if c}

    for row in rows:
        event_time = _parse_event_time(row["event_time"])
        if event_time is None:
            continue

        minutes = abs((event_time - now).total_seconds()) / 60
        if minutes > window_minutes:
            continue

        currency = (row["country"] or "").strip().upper()
        if relevant is not None and currency and currency not in relevant:
            continue

        direction = "daqui a" if event_time >= now else "há"
        return {
            "dangerous_event_nearby": True,
            "dangerous_event_reason": (
                f"evento high impact {direction} {round(minutes)} min: "
                f"{row['country']} {row['title']}"
            ),
        }

    return {
        "dangerous_event_nearby": False,
        "dangerous_event_reason": "",
    }


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
        SELECT signal, confidence, reasoning, risk_level, hold_off, provider
        FROM ai_analyses
        WHERE pair = ? AND analysis_date = ? AND input_hash = ? AND provider = ?
        """,
        (pair, analysis_date, input_hash, provider),
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["hold_off"] = bool(result["hold_off"])
    return result


def save_ai_analysis(conn, pair, analysis_date, input_hash, result):
    conn.execute(
        """
        INSERT OR IGNORE INTO ai_analyses
        (pair, analysis_date, input_hash, signal, confidence, reasoning, risk_level, hold_off, provider, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            utc_now(),
        ),
    )
    conn.commit()


def save_decision(conn, entry):
    conn.execute(
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
         stop_loss_pips_used, take_profit_pips_used, sl_tp_mode, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            entry.get("stop_loss_pips_used"),
            entry.get("take_profit_pips_used"),
            entry.get("sl_tp_mode"),
            utc_now(),
        ),
    )
    conn.commit()


def get_last_decision_signature(conn, pair):
    row = conn.execute(
        """
        SELECT decision_signature, timestamp
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
