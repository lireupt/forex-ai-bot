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
        "AI_PROVIDER", "GROQ_API_KEY", "ANTHROPIC_API_KEY",
        "DRY_RUN", "MIN_CONFIDENCE", "ACCOUNT_BALANCE",
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
