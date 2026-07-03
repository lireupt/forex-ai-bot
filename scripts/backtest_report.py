"""Relatório de um backtest run — texto e, opcionalmente, CSV em logs/.

Uso:
    python scripts/backtest_report.py --run-id <run_id>
    python scripts/backtest_report.py --run-id <run_id> --csv
"""

import argparse
import csv
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules import analytics_metrics, database  # noqa: E402

SESSIONS = [
    ("Sydney/Tokyo", 0, 7),
    ("London", 7, 13),
    ("London/NY overlap", 13, 16),
    ("New York", 16, 21),
    ("Late NY/Pré-Ásia", 21, 24),
]


def _session_for_hour(hour):
    for name, start, end in SESSIONS:
        if start <= hour < end:
            return name
    return "unknown"


def _longest_losing_streak(trades):
    longest = current = 0
    for trade in trades:
        if trade["status"] == "loss":
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def _monthly_breakdown(closed):
    buckets = defaultdict(lambda: {"trades": 0, "wins": 0, "losses": 0, "pips": 0.0})
    for trade in closed:
        month = trade["created_at"][:7]
        bucket = buckets[month]
        bucket["trades"] += 1
        bucket["wins"] += 1 if trade["status"] == "win" else 0
        bucket["losses"] += 1 if trade["status"] == "loss" else 0
        bucket["pips"] += trade.get("result_pips") or 0.0
    rows = []
    for month in sorted(buckets):
        bucket = buckets[month]
        winrate = round(bucket["wins"] / bucket["trades"] * 100, 1) if bucket["trades"] else None
        rows.append({"month": month, "winrate": winrate, "pips": round(bucket["pips"], 1), **bucket})
    return rows


def _session_breakdown(closed):
    buckets = defaultdict(lambda: {"trades": 0, "wins": 0, "losses": 0, "pips": 0.0})
    for trade in closed:
        dt = datetime.fromisoformat(trade["created_at"].replace("Z", "+00:00"))
        session = _session_for_hour(dt.hour)
        bucket = buckets[session]
        bucket["trades"] += 1
        bucket["wins"] += 1 if trade["status"] == "win" else 0
        bucket["losses"] += 1 if trade["status"] == "loss" else 0
        bucket["pips"] += trade.get("result_pips") or 0.0
    rows = []
    for name, _start, _end in SESSIONS:
        if name not in buckets:
            continue
        bucket = buckets[name]
        winrate = round(bucket["wins"] / bucket["trades"] * 100, 1) if bucket["trades"] else None
        rows.append({"session": name, "winrate": winrate, "pips": round(bucket["pips"], 1), **bucket})
    return rows


def _blocking_reason_distribution(decisions):
    counter = Counter((d.get("blocking_reason") or "(none)") for d in decisions)
    total = len(decisions)
    return [
        {"reason": reason, "count": count, "pct": round(count / total * 100, 1) if total else None}
        for reason, count in counter.most_common()
    ]


def build_report(conn, run_id):
    run = database.get_backtest_run(conn, run_id)
    if run is None:
        raise ValueError(f"run_id desconhecido: {run_id!r}")

    trades = database.get_backtest_trades(conn, run_id)
    decisions = database.get_backtest_decisions(conn, run_id)

    closed = [t for t in trades if t["status"] in ("win", "loss")]
    expired = [t for t in trades if t["status"] == "expired"]
    open_trades = [t for t in trades if t["status"] == "open"]

    metrics = analytics_metrics.compute_metrics(closed, [])
    total_pips = round(sum(t.get("result_pips") or 0.0 for t in closed), 1)

    return {
        "run": run,
        "total_trades": len(trades),
        "closed_trades": len(closed),
        "expired_trades": len(expired),
        "open_trades": len(open_trades),
        "winrate": metrics["winrate"],
        "profit_factor": metrics["profit_factor"],
        "expectancy_r": metrics["expectancy"],
        "total_pips": total_pips,
        "max_drawdown_r": metrics["max_drawdown"],
        "longest_losing_streak": _longest_losing_streak(trades),
        "monthly": _monthly_breakdown(closed),
        "sessions": _session_breakdown(closed),
        "blocking_reasons": _blocking_reason_distribution(decisions),
    }


def _print_text_report(report):
    run = report["run"]
    print(f"=== Backtest Report — run_id={run['run_id']} ===")
    print(f"Par: {run['pair']} | {run['date_from']} -> {run['date_to']}")
    print(
        f"Status: {run['status']} | candles={run.get('total_candles')} "
        f"decisões={run.get('total_decisions')}"
    )
    print()
    print(
        f"Trades: {report['total_trades']} "
        f"(fechadas={report['closed_trades']}, expiradas={report['expired_trades']}, "
        f"abertas={report['open_trades']})"
    )
    print(f"Win rate: {report['winrate']}%")
    print(f"Profit factor: {report['profit_factor']}")
    print(f"Expectância: {report['expectancy_r']} R")
    print(f"Total pips: {report['total_pips']}")
    print(f"Max drawdown: {report['max_drawdown_r']} R")
    print(f"Maior sequência de perdas: {report['longest_losing_streak']}")
    print()
    print("Breakdown mensal:")
    for row in report["monthly"]:
        print(f"  {row['month']}: {row['trades']} trades, winrate={row['winrate']}%, pips={row['pips']}")
    print()
    print("Breakdown por sessão (UTC):")
    for row in report["sessions"]:
        print(f"  {row['session']}: {row['trades']} trades, winrate={row['winrate']}%, pips={row['pips']}")
    print()
    print("Distribuição de blocking_reason:")
    for row in report["blocking_reasons"]:
        print(f"  {row['reason']}: {row['count']} ({row['pct']}%)")


def _write_csv(rows, path):
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def export_csv(report, out_dir=None):
    out_dir = out_dir or Path("logs")
    run_id = report["run"]["run_id"]
    summary_row = {
        "run_id": run_id,
        "pair": report["run"]["pair"],
        "date_from": report["run"]["date_from"],
        "date_to": report["run"]["date_to"],
        "total_trades": report["total_trades"],
        "closed_trades": report["closed_trades"],
        "winrate": report["winrate"],
        "profit_factor": report["profit_factor"],
        "expectancy_r": report["expectancy_r"],
        "total_pips": report["total_pips"],
        "max_drawdown_r": report["max_drawdown_r"],
        "longest_losing_streak": report["longest_losing_streak"],
    }
    _write_csv([summary_row], out_dir / f"backtest_report_{run_id}_summary.csv")
    _write_csv(report["monthly"], out_dir / f"backtest_report_{run_id}_monthly.csv")
    _write_csv(report["sessions"], out_dir / f"backtest_report_{run_id}_sessions.csv")
    _write_csv(report["blocking_reasons"], out_dir / f"backtest_report_{run_id}_blocking_reasons.csv")
    return out_dir


def main():
    parser = argparse.ArgumentParser(description="Relatório de um backtest run.")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--csv", action="store_true", help="Também exporta CSV para logs/.")
    args = parser.parse_args()

    conn = database.connect()
    try:
        report = build_report(conn, args.run_id)
    finally:
        conn.close()

    _print_text_report(report)
    if args.csv:
        out_dir = export_csv(report)
        print(f"\nCSV exportado para {out_dir}/")


if __name__ == "__main__":
    main()
