"""Fixtures partilhadas entre todos os testes.

Usamos uma DB SQLite in-memory por teste para garantir isolamento total.
Não tocamos em `data/forex_bot.db` (real) e ignoramos `.env` para não puxar
chaves nem provider configurado por engano.
"""

import os
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Isola env vars relevantes para cada teste — evita herdar .env."""
    keys = [
        "AI_PROVIDER", "GROQ_API_KEY", "ANTHROPIC_API_KEY", "TIMEFRAME",
        "DRY_RUN", "MIN_CONFIDENCE", "ACCOUNT_BALANCE",
        "ADAPTIVE_BASE_MIN_CONFIDENCE", "ADAPTIVE_MIN_FLOOR",
        "ADAPTIVE_MIN_CEILING", "MAX_SPREAD_PIPS", "ATR_EXTREME_PIPS",
        "BLOCK_EXTREME_NEWS_RISK",
        "RISK_PER_TRADE_PERCENT", "DEFAULT_STOP_LOSS_PIPS",
        "DEFAULT_TAKE_PROFIT_PIPS", "USE_CACHE", "FORCE_REFRESH",
        "BLOCK_NEAR_HIGH_IMPACT_EVENTS", "EVENT_BLOCK_WINDOW_MINUTES",
        "USE_ATR_SL_TP", "ATR_SL_MULT", "ATR_TP_MULT",
        "ATR_MIN_SL_PIPS", "ATR_MAX_SL_PIPS",
        "SCORE_BUY_THRESHOLD", "SCORE_SELL_THRESHOLD",
        "SCORE_AI_WEIGHT", "SCORE_TECHNICAL_WEIGHT", "SCORE_SHADOW_WEIGHT",
        "PAPER_TRADE_SL_MULT", "PAPER_TRADE_TP_MULT",
        "PAPER_TRADE_EXPIRY_BARS", "GATING_MODE",
        "GATE_MIN_TRADES", "GATE_MIN_PROFIT_FACTOR", "GATE_MIN_AVG_R",
        "GATE_MIN_WIN_RATE", "GATE_MAX_STREAK_LOSSES",
        "GATE_MAX_DRAWDOWN_PCT",
        "BOT_MODE", "TRADE_WINDOW_TOLERANCE_MINUTES",
        "OPERATIONAL_TRADE_START_HOUR", "OPERATIONAL_TRADE_END_HOUR",
        "COOLDOWN_MINUTES", "MAX_DIRECTION_SIGNALS_PER_DAY",
        "MIN_CONFIDENCE_TO_TRADE",
        "AI_AGGREGATOR_ENABLED", "AI_AGGREGATOR_PROVIDER", "AI_AGGREGATOR_MODE",
        "AGGREGATOR_ADVISORY_MIN_TRADES", "AGGREGATOR_ADVISORY_MIN_PER_GROUP",
        "WEEKEND_MODE_ENABLED", "WEEKEND_MODE_UPDATE_NEWS",
        "WEEKEND_MODE_UPDATE_CALENDAR", "WEEKEND_MODE_EXPORT_LOGS",
        "WEEKLY_MARKET_PREP_ENABLED", "WEEKLY_MARKET_PREP_WEEKDAY",
        "WEEKLY_MARKET_PREP_HOUR_UTC", "WEEKLY_MARKET_PREP_LOOKBACK_HOURS",
        "WEEKLY_MARKET_PREP_CALENDAR_DAYS", "WEEKLY_MARKET_PREP_USE_AI",
        "FOREX_MARKET_GUARD_ENABLED", "FOREX_MARKET_CLOSE_WEEKDAY",
        "FOREX_MARKET_CLOSE_HOUR_UTC", "FOREX_MARKET_OPEN_WEEKDAY",
        "FOREX_MARKET_OPEN_HOUR_UTC",
        "ROLLING_CONTEXT_ENABLED", "ROLLING_CONTEXT_UPDATE_EVERY_CYCLE",
        "ROLLING_CONTEXT_LOOKBACK_HOURS", "ROLLING_CONTEXT_MAX_PREVIOUS_SUMMARY_CHARS",
        "ROLLING_CONTEXT_PROVIDER",
        "USE_ECONOMIC_CALENDAR_FILTER",
        "MACRO_HIGH_IMPACT_BLOCK_BEFORE_MINUTES",
        "MACRO_HIGH_IMPACT_BLOCK_AFTER_MINUTES",
        "MACRO_MEDIUM_IMPACT_WINDOW_MINUTES",
        "MACRO_MEDIUM_IMPACT_CONFIDENCE_FACTOR",
    ]
    for key in keys:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def memory_db(monkeypatch, tmp_path):
    """Patch DB_PATH para apontar para um ficheiro temporário e devolve
    a connection inicializada (com schema completo)."""
    from modules import database

    db_file = tmp_path / "test.db"
    monkeypatch.setattr(database, "DB_PATH", db_file)
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    database.init_db(conn)
    yield conn
    conn.close()
