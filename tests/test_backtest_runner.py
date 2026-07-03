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


class TestRowsToDf:
    """Regressão: uma candle 1d gravada por outra parte do sistema (provider
    'yahoo') com candle_time sem offset de fuso partia pd.to_datetime()
    quando misturada com linhas com offset — descoberto ao correr o
    backtest sobre dados reais."""

    def test_handles_mixed_naive_and_aware_timestamps(self):
        rows = [
            {"candle_time": "2025-05-20T00:00:00+00:00", "open": 1.1, "high": 1.11, "low": 1.09, "close": 1.1, "volume": 0},
            {"candle_time": "2025-05-21T00:00:00", "open": 1.1, "high": 1.11, "low": 1.09, "close": 1.1, "volume": 0},
        ]
        df = br._rows_to_df(rows)
        assert len(df) == 2
        assert str(df.index.tz) == "UTC"


class TestHistoricalProviderCandleFilter:
    def test_candle_provider_filters_out_other_sources(self, memory_db):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        clean = _synthetic_hourly_candles(10, start)
        database.save_market_candles(memory_db, clean, "EUR/USD", "1h", "import")
        dirty = [{
            "candle_time": (start + timedelta(hours=5)).isoformat(),
            "open": 9.9, "high": 9.9, "low": 9.9, "close": 9.9, "volume": 0,
        }]
        database.save_market_candles(memory_db, dirty, "EUR/USD", "1h", "yahoo")
        memory_db.commit()

        provider_filtered = br.HistoricalProvider(memory_db, "EUR/USD", candle_provider="import")
        rows = provider_filtered.driving_candles("1h", start.isoformat(), (start + timedelta(hours=20)).isoformat())
        assert len(rows) == 10  # só as "import", não a "yahoo" extra

        provider_unfiltered = br.HistoricalProvider(memory_db, "EUR/USD")
        rows_all = provider_unfiltered.driving_candles("1h", start.isoformat(), (start + timedelta(hours=20)).isoformat())
        assert len(rows_all) == 11  # sem filtro, vê as 10 "import" + 1 "yahoo" extra
