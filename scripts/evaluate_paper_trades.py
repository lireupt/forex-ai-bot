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
from modules.trade_simulator import (  # noqa: E402
    compute_r_multiple as _compute_r_multiple,
    signed_pips as _signed_pips_raw,
    simulate_trade,
)

_PAIR_SPEC = get_pair_spec("EUR/USD")
PIP_SIZE = _PAIR_SPEC.pip_size


def _signed_pips(direction, entry, exit_price):
    return _signed_pips_raw(direction, entry, exit_price, PIP_SIZE)


def _evaluate_trade(trade, candles):
    result = simulate_trade(trade, candles, _PAIR_SPEC, apply_spread=False)
    if result is None:
        return None
    return {
        "status": result.status,
        "close_price": result.close_price,
        "closed_at": result.closed_at,
        "close_reason": result.close_reason,
        "result_pips": result.result_pips,
        "result_r_multiple": result.result_r_multiple,
    }


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
