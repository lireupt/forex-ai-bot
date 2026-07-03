"""Teste de fumo para `backtest_runner` — corre ponta-a-ponta sobre candles
sintéticas e confirma isolamento de dados (nunca escreve em paper_trades/
decisions) e que produz um run completo com decisões registadas."""

from datetime import datetime, timedelta, timezone

import pytest

import backtest_runner as br
from modules import database


def _synthetic_hourly_candles(n, start):
    candles = []
    price = 1.1000
    for i in range(n):
        # passeio aleatório determinístico (sem numpy/random) para ter
        # alguma variação sem depender de seed externa.
        drift = 0.00005 * ((i % 7) - 3)
        price = round(price + drift, 5)
        high = round(price + 0.0006, 5)
        low = round(price - 0.0006, 5)
        candles.append({
            "candle_time": (start + timedelta(hours=i)).isoformat(),
            "open": price, "high": high, "low": low, "close": price,
            "volume": 100.0,
        })
    return candles


class TestRunBacktestSmoke:
    def test_runs_end_to_end_and_isolates_production_tables(self, memory_db, monkeypatch, tmp_path):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        candles = _synthetic_hourly_candles(150, start)
        database.save_market_candles(memory_db, candles, "EUR/USD", "1h", "import")
        memory_db.commit()

        # backtest_runner abre a sua própria connection via database.connect();
        # a fixture memory_db já monkeypatchou database.DB_PATH para o mesmo
        # ficheiro temporário, por isso não é preciso passar --db aqui.
        date_from = (start + timedelta(hours=100)).isoformat()
        date_to = (start + timedelta(hours=120)).isoformat()

        stats = br.run_backtest("EUR/USD", date_from, date_to, config={"apply_spread": True})

        assert stats["total_candles"] == 21  # 100..120 inclusive, 1 candle/hora
        assert stats["total_decisions"] == stats["total_candles"]

        conn = database.connect()
        run_row = database.get_backtest_run(conn, stats["run_id"])
        assert run_row["status"] == "completed"
        assert run_row["pair"] == "EUR/USD"

        decisions = database.get_backtest_decisions(conn, stats["run_id"])
        assert len(decisions) == stats["total_decisions"]

        # Isolamento: nada foi escrito nas tabelas de produção.
        prod_trades = conn.execute("SELECT COUNT(*) AS n FROM paper_trades").fetchone()
        prod_decisions = conn.execute("SELECT COUNT(*) AS n FROM decisions").fetchone()
        assert prod_trades["n"] == 0
        assert prod_decisions["n"] == 0
        conn.close()

    def test_raises_when_no_candles_available(self, memory_db):
        with pytest.raises(ValueError):
            br.run_backtest(
                "EUR/USD",
                "2024-01-01T00:00:00+00:00",
                "2024-01-02T00:00:00+00:00",
            )
