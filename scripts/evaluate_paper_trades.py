"""Avalia paper-trades em aberto e marca-as como win/loss/expired.

Para cada paper-trade aberta, percorre as candles cronológicas entre
`created_at` e `expiry_at` (fonte: market_candles em SQLite, fallback para
yfinance se necessário) e verifica se o preço tocou no SL ou TP. Se a expiry
for atingida sem hit, marca como `expired`.

Uso:
    python scripts/evaluate_paper_trades.py
    python scripts/evaluate_paper_trades.py --pair EUR/USD --timeframe 1h
"""

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules import database  # noqa: E402
from modules.pair_spec import get_pair_spec  # noqa: E402

PIP_SIZE = get_pair_spec("EUR/USD").pip_size


def _parse_iso(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _signed_pips(direction, entry, exit_price):
    delta = exit_price - entry
    if direction == "SELL":
        delta = -delta
    return round(delta / PIP_SIZE, 1)


def _compute_r_multiple(direction, entry, exit_price, sl_price):
    risk = abs(entry - sl_price)
    if risk == 0:
        return None
    delta = exit_price - entry
    if direction == "SELL":
        delta = -delta
    return round(delta / risk, 2)


def _evaluate_trade(trade, candles):
    direction = trade["direction"]
    entry = float(trade["entry_price"])
    sl = float(trade["simulated_sl"])
    tp = float(trade["simulated_tp"])

    expiry_dt = _parse_iso(trade.get("expiry_at"))
    now = datetime.now(timezone.utc)

    for candle in candles:
        candle_time = _parse_iso(candle["candle_time"])
        if candle_time is None:
            continue
        high = float(candle["high"])
        low = float(candle["low"])

        hit_tp = (direction == "BUY" and high >= tp) or (direction == "SELL" and low <= tp)
        hit_sl = (direction == "BUY" and low <= sl) or (direction == "SELL" and high >= sl)

        if hit_tp and hit_sl:
            close_price = sl
            status = "loss"
            reason = "SL e TP na mesma candle — assumido SL primeiro"
        elif hit_tp:
            close_price = tp
            status = "win"
            reason = "TP atingido"
        elif hit_sl:
            close_price = sl
            status = "loss"
            reason = "SL atingido"
        else:
            continue

        return {
            "status": status,
            "close_price": round(close_price, 5),
            "closed_at": candle_time.isoformat(),
            "close_reason": reason,
            "result_pips": _signed_pips(direction, entry, close_price),
            "result_r_multiple": _compute_r_multiple(direction, entry, close_price, sl),
        }

    if expiry_dt is not None and expiry_dt <= now:
        last_close = float(candles[-1]["close"]) if candles else entry
        return {
            "status": "expired",
            "close_price": round(last_close, 5),
            "closed_at": expiry_dt.isoformat(),
            "close_reason": "expirou sem atingir SL/TP",
            "result_pips": _signed_pips(direction, entry, last_close),
            "result_r_multiple": _compute_r_multiple(direction, entry, last_close, sl),
        }

    return None


def evaluate(pair=None, timeframe=None):
    conn = database.connect()
    database.init_db(conn)
    open_trades = database.get_open_paper_trades(conn, pair=pair)

    updated = 0
    skipped = 0
    for trade in open_trades:
        if timeframe and trade.get("timeframe") != timeframe:
            skipped += 1
            continue

        candles = database.get_market_candles_between(
            conn,
            pair=trade["pair"],
            timeframe=trade["timeframe"],
            start_iso=trade["created_at"],
            end_iso=trade.get("expiry_at") or datetime.now(timezone.utc).isoformat(),
        )

        result = _evaluate_trade(trade, candles)
        if result is None:
            skipped += 1
            continue

        database.update_paper_trade_result(
            conn,
            paper_trade_id=trade["id"],
            status=result["status"],
            close_price=result["close_price"],
            closed_at=result["closed_at"],
            close_reason=result["close_reason"],
            result_pips=result["result_pips"],
            result_r_multiple=result["result_r_multiple"],
        )
        updated += 1
        print(
            f"[#{trade['id']}] {trade['direction']} {trade['pair']} "
            f"@ {trade['entry_price']} -> {result['status']} "
            f"({result['result_pips']:+.1f} pips, R={result['result_r_multiple']})"
        )

    conn.close()
    return {"updated": updated, "skipped": skipped, "total_open_before": len(open_trades)}


def main():
    parser = argparse.ArgumentParser(description="Avalia paper-trades em aberto.")
    parser.add_argument("--pair", default=None)
    parser.add_argument("--timeframe", default=None)
    args = parser.parse_args()

    try:
        stats = evaluate(pair=args.pair, timeframe=args.timeframe)
    except sqlite3.Error as e:
        print(f"[evaluate_paper_trades] erro DB: {e}", file=sys.stderr)
        sys.exit(1)

    print(
        f"[evaluate_paper_trades] {stats['updated']} fechados / "
        f"{stats['skipped']} ainda em aberto (de {stats['total_open_before']})."
    )


if __name__ == "__main__":
    main()
