"""Testes para `scripts.import_history` — parsing, agregação, gaps, dedupe."""

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from scripts import import_history as ih


class TestReadHistdata1min:
    def test_parses_semicolon_format_and_converts_to_utc(self, tmp_path):
        # 20240102 090000 EST == 2024-01-02 14:00:00 UTC (EST = UTC-5, sem DST)
        csv_path = tmp_path / "eurusd_m1.csv"
        csv_path.write_text(
            "20240102 090000;1.10000;1.10050;1.09990;1.10020;100\n"
            "20240102 090100;1.10020;1.10030;1.10000;1.10010;80\n"
        )
        df = ih._read_histdata_1min(csv_path)
        assert len(df) == 2
        assert df.index[0] == pd.Timestamp("2024-01-02 14:00:00", tz="UTC")
        assert df.iloc[0]["close"] == 1.10020


class TestAggregateTo:
    def test_ohlc_aggregation_rules(self):
        idx = pd.to_datetime([
            "2024-01-02 14:00:00", "2024-01-02 14:01:00", "2024-01-02 14:59:00",
        ]).tz_localize("UTC")
        df = pd.DataFrame({
            "open": [1.1000, 1.1001, 1.1010],
            "high": [1.1005, 1.1020, 1.1015],
            "low": [1.0995, 1.1000, 1.1005],
            "close": [1.1001, 1.1010, 1.1012],
            "volume": [10, 20, 30],
        }, index=idx)
        hourly = ih._aggregate_to(df, "1h")
        assert len(hourly) == 1
        row = hourly.iloc[0]
        assert row["open"] == 1.1000       # primeira candle do minuto
        assert row["high"] == 1.1020       # máximo da hora
        assert row["low"] == 1.0995        # mínimo da hora
        assert row["close"] == 1.1012      # última candle do minuto
        assert row["volume"] == 60

    def test_different_rules_produce_different_bar_counts(self):
        idx = pd.date_range("2024-01-02 00:00:00", periods=8 * 60, freq="1min", tz="UTC")
        df = pd.DataFrame({
            "open": 1.10, "high": 1.11, "low": 1.09, "close": 1.10, "volume": 1.0,
        }, index=idx)
        assert len(ih._aggregate_to(df, "15min")) == 32   # 8h / 15min
        assert len(ih._aggregate_to(df, "1h")) == 8
        assert len(ih._aggregate_to(df, "4h")) == 2
        assert len(ih._aggregate_to(df, "1D")) == 1


class TestReadGenericOhlcv:
    def test_naive_timestamps_localized_with_tz_arg(self, tmp_path):
        csv_path = tmp_path / "eurusd_1h.csv"
        csv_path.write_text(
            "datetime,open,high,low,close,volume\n"
            "2024-01-02 09:00:00,1.1000,1.1010,1.0990,1.1005,1000\n"
        )
        df = ih._read_generic_ohlcv(csv_path, tz="UTC")
        assert df.index[0] == pd.Timestamp("2024-01-02 09:00:00", tz="UTC")

    def test_already_tz_aware_timestamps_converted(self, tmp_path):
        csv_path = tmp_path / "eurusd_1h_tz.csv"
        csv_path.write_text(
            "datetime,open,high,low,close,volume\n"
            "2024-01-02T09:00:00-05:00,1.1000,1.1010,1.0990,1.1005,1000\n"
        )
        df = ih._read_generic_ohlcv(csv_path, tz="UTC")
        assert df.index[0] == pd.Timestamp("2024-01-02 14:00:00", tz="UTC")


class TestFindGaps:
    def test_gap_during_business_days_is_reported(self):
        index = pd.DatetimeIndex([
            pd.Timestamp("2024-01-02 09:00:00", tz="UTC"),
            pd.Timestamp("2024-01-02 15:00:00", tz="UTC"),  # 6h de buraco, mesma terça
        ])
        gaps = ih.find_gaps(index)
        assert len(gaps) == 1
        assert gaps[0][2] == 6.0

    def test_normal_weekend_closure_not_reported(self):
        index = pd.DatetimeIndex([
            pd.Timestamp("2024-01-05 21:00:00", tz="UTC"),  # sexta
            pd.Timestamp("2024-01-07 22:00:00", tz="UTC"),  # domingo
        ])
        gaps = ih.find_gaps(index)
        assert gaps == []

    def test_small_gap_under_threshold_not_reported(self):
        index = pd.DatetimeIndex([
            pd.Timestamp("2024-01-02 09:00:00", tz="UTC"),
            pd.Timestamp("2024-01-02 11:00:00", tz="UTC"),
        ])
        assert ih.find_gaps(index) == []


class TestImportHistoryIntegration:
    def test_import_ohlcv_writes_candles_and_dedupes_on_rerun(self, memory_db, monkeypatch, tmp_path):
        from modules import database

        csv_path = tmp_path / "eurusd_1h.csv"
        csv_path.write_text(
            "datetime,open,high,low,close,volume\n"
            "2024-01-02 09:00:00,1.1000,1.1010,1.0990,1.1005,1000\n"
            "2024-01-02 10:00:00,1.1005,1.1020,1.1000,1.1015,1200\n"
        )

        stats = ih.import_history(csv_path, "EUR/USD", "ohlcv", timeframe="1h")
        assert stats["timeframes"]["1h"]["imported"] == 2

        rows = memory_db.execute(
            "SELECT COUNT(*) AS n FROM market_candles WHERE pair = ? AND timeframe = ?",
            ("EUR/USD", "1h"),
        ).fetchone()
        assert rows["n"] == 2

        # Reimportar o mesmo ficheiro não deve duplicar linhas (INSERT OR
        # REPLACE por (pair, timeframe, candle_time, provider)).
        ih.import_history(csv_path, "EUR/USD", "ohlcv", timeframe="1h")
        rows_after = memory_db.execute(
            "SELECT COUNT(*) AS n FROM market_candles WHERE pair = ? AND timeframe = ?",
            ("EUR/USD", "1h"),
        ).fetchone()
        assert rows_after["n"] == 2

    def test_import_does_not_fabricate_missing_candles(self, memory_db, tmp_path):
        csv_path = tmp_path / "eurusd_1h_gap.csv"
        csv_path.write_text(
            "datetime,open,high,low,close,volume\n"
            "2024-01-02 09:00:00,1.1000,1.1010,1.0990,1.1005,1000\n"
            "2024-01-02 15:00:00,1.1005,1.1020,1.1000,1.1015,1200\n"
        )
        stats = ih.import_history(csv_path, "EUR/USD", "ohlcv", timeframe="1h")
        assert stats["timeframes"]["1h"]["imported"] == 2
        assert len(stats["timeframes"]["1h"]["gaps"]) == 1

    def test_histdata1m_populates_all_four_timeframes(self, memory_db, tmp_path):
        """Regressão: importar só 1h deixava M15/H4/D1 permanentemente
        NEUTRAL no backtest (candles_by_timeframe sempre vazio)."""
        csv_path = tmp_path / "eurusd_m1.csv"
        start = datetime(2024, 1, 2, 9, 0, tzinfo=timezone.utc)  # EST -> 14:00 UTC
        lines = []
        price = 1.1000
        for i in range(10 * 60):  # 10h de candles de 1 minuto
            ts = start + timedelta(minutes=i)
            price = round(price + 0.00001, 5)
            lines.append(f"{ts.strftime('%Y%m%d %H%M%S')};{price};{price+0.0002};{price-0.0002};{price};10")
        csv_path.write_text("\n".join(lines) + "\n")

        stats = ih.import_history(csv_path, "EUR/USD", "histdata1m")
        assert set(stats["timeframes"].keys()) == {"15m", "1h", "4h", "1d"}
        assert stats["timeframes"]["1h"]["imported"] > 0
        assert stats["timeframes"]["15m"]["imported"] > 0

        for tf in ("15m", "1h", "4h", "1d"):
            rows = memory_db.execute(
                "SELECT COUNT(*) AS n FROM market_candles WHERE pair = ? AND timeframe = ?",
                ("EUR/USD", tf),
            ).fetchone()
            assert rows["n"] > 0, f"timeframe {tf} deveria ter candles importadas"
