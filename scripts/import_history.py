"""Importador de candles históricas para `market_candles`.

Suporta dois formatos de origem:
- `histdata1m`: HistData.com "Generic ASCII" 1-minuto —
  `YYYYMMDD HHMMSS;OPEN;HIGH;LOW;CLOSE;VOLUME`, sem cabeçalho, timestamps em
  EST fixo (UTC-5, sem horário de verão — convenção HistData). Agregado
  para 1h antes de gravar.
- `ohlcv`: CSV genérico com cabeçalho `datetime,open,high,low,close,volume`
  (datetime em ISO ou `YYYY-MM-DD HH:MM:SS`), já na timeframe pedida — sem
  agregação.

Uso:
    python scripts/import_history.py --file EURUSD_2024_M1.csv --pair EUR/USD --format histdata1m
    python scripts/import_history.py --file eurusd_1h.csv --pair EUR/USD --format ohlcv --timeframe 1h

Nunca inventa candles para preencher buracos — só reporta os que excedem
3h e não correspondem ao fecho semanal normal do mercado forex.
"""

import argparse
import sys
from datetime import timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules import database  # noqa: E402

HISTDATA_TZ = "Etc/GMT+5"  # EST fixo (sem DST) — convenção "Generic ASCII" da HistData
GAP_REPORT_HOURS = 3


def _read_histdata_1min(path):
    df = pd.read_csv(
        path, sep=";", header=None,
        names=["datetime", "open", "high", "low", "close", "volume"],
        dtype={"datetime": str},
    )
    df["datetime"] = pd.to_datetime(df["datetime"], format="%Y%m%d %H%M%S")
    df = df.set_index("datetime")
    df.index = df.index.tz_localize(HISTDATA_TZ).tz_convert("UTC")
    return df[["open", "high", "low", "close", "volume"]].astype(float)


def _read_generic_ohlcv(path, tz):
    df = pd.read_csv(path)
    df.columns = [c.strip().lower() for c in df.columns]
    time_col = "datetime" if "datetime" in df.columns else df.columns[0]
    df[time_col] = pd.to_datetime(df[time_col])
    df = df.set_index(time_col)
    if df.index.tz is None:
        df.index = df.index.tz_localize(tz)
    df.index = df.index.tz_convert("UTC")
    return df[["open", "high", "low", "close", "volume"]].astype(float)


def _aggregate_to_hourly(df):
    agg = df.resample("1h").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    })
    return agg.dropna(subset=["open", "high", "low", "close"])


def _is_normal_weekend_closure(prev, curr):
    """O forex fecha sexta ~22:00 UTC e reabre domingo ~22:00 UTC — não é
    um buraco de dados, é o mercado fechado."""
    return prev.weekday() == 4 and curr.weekday() in (6, 0) and (curr - prev) <= timedelta(hours=60)


def find_gaps(index, max_gap_hours=GAP_REPORT_HOURS):
    gaps = []
    ordered = index.sort_values()
    for prev, curr in zip(ordered[:-1], ordered[1:]):
        delta = curr - prev
        if delta.total_seconds() / 3600 <= max_gap_hours:
            continue
        if _is_normal_weekend_closure(prev, curr):
            continue
        gaps.append((prev, curr, round(delta.total_seconds() / 3600, 1)))
    return gaps


def _df_to_candle_dicts(df):
    candles = []
    for ts, row in df.iterrows():
        candles.append({
            "candle_time": ts.isoformat(),
            "open": row["open"],
            "high": row["high"],
            "low": row["low"],
            "close": row["close"],
            "volume": row["volume"],
        })
    return candles


def import_history(file_path, pair, fmt, timeframe="1h", tz="UTC", provider="import"):
    if fmt == "histdata1m":
        raw = _read_histdata_1min(file_path)
        df = _aggregate_to_hourly(raw)
        timeframe = "1h"
    elif fmt == "ohlcv":
        df = _read_generic_ohlcv(file_path, tz)
    else:
        raise ValueError(f"formato desconhecido: {fmt!r} (usa 'histdata1m' ou 'ohlcv')")

    df = df[~df.index.duplicated(keep="last")].sort_index()
    gaps = find_gaps(df.index)

    conn = database.connect()
    database.init_db(conn)
    candles = _df_to_candle_dicts(df)
    database.save_market_candles(conn, candles, pair, timeframe, provider)
    conn.close()

    return {"imported": len(candles), "gaps": gaps, "pair": pair, "timeframe": timeframe}


def main():
    parser = argparse.ArgumentParser(description="Importa candles históricas para market_candles.")
    parser.add_argument("--file", required=True)
    parser.add_argument("--pair", required=True)
    parser.add_argument("--format", required=True, choices=["histdata1m", "ohlcv"])
    parser.add_argument("--timeframe", default="1h", help="Só usado com --format ohlcv.")
    parser.add_argument("--tz", default="UTC", help="Timezone de origem para --format ohlcv.")
    parser.add_argument("--provider", default="import")
    args = parser.parse_args()

    stats = import_history(
        args.file, args.pair, args.format,
        timeframe=args.timeframe, tz=args.tz, provider=args.provider,
    )
    print(f"[import_history] {stats['imported']} candles importadas para {stats['pair']} {stats['timeframe']}.")
    if stats["gaps"]:
        print(f"[import_history] {len(stats['gaps'])} buraco(s) > {GAP_REPORT_HOURS}h fora do fecho semanal:")
        for prev, curr, hours in stats["gaps"]:
            print(f"  {prev.isoformat()} -> {curr.isoformat()} ({hours}h)")
    else:
        print(f"[import_history] sem buracos > {GAP_REPORT_HOURS}h fora do fecho semanal.")


if __name__ == "__main__":
    main()
