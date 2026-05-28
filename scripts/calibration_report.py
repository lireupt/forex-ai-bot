"""Resumo de calibração para decisões e paper trades em SQLite.

Uso:
    python scripts/calibration_report.py
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules import database  # noqa: E402


def _since(hours=None, days=None):
    delta = timedelta(hours=hours or 0, days=days or 0)
    return (datetime.now(timezone.utc) - delta).isoformat()


def _fmt(value, suffix=""):
    if value is None:
        return "-"
    return f"{value}{suffix}"


def _print_section(title, summary):
    print(f"\n=== {title} ===")
    print(f"total_decisions: {summary['total_decisions']}")
    print(f"total_blocked:   {summary['total_blocked']} ({_fmt(summary['block_rate'], '%')})")
    print(f"total_executed:  {summary['total_executed']}")
    print(f"wins/losses:     {summary['wins']} / {summary['losses']}")
    print(f"winrate:         {_fmt(summary['winrate'], '%')}")
    print(f"avg_confidence:  {_fmt(summary['avg_confidence'], '%')}")
    print(f"avg_pips:        {_fmt(summary['avg_pips'])}")
    print(f"net_pips:        {_fmt(summary['net_pips'])}")
    print(f"profit_factor:   {_fmt(summary['profit_factor'])}")
    print(f"expectancy:      {_fmt(summary['expectancy'])}")
    print(
        "buy_vs_sell:     "
        f"BUY={summary['buy_vs_sell']['BUY']} / SELL={summary['buy_vs_sell']['SELL']}"
    )
    print(f"best_direction:  {_fmt(summary['best_direction'])}")
    if summary["blocked_by_reason"]:
        print("blocked_by_reason:")
        for reason, count in summary["blocked_by_reason"].items():
            print(f"  - {reason}: {count}")
    else:
        print("blocked_by_reason: -")


def main():
    conn = database.connect()
    database.init_db(conn)
    try:
        sections = (
            ("ultimas_24h", _since(hours=24)),
            ("ultimos_7_dias", _since(days=7)),
            ("total_historico", None),
        )
        print(f"DB: {database.DB_PATH}")
        for title, since_iso in sections:
            _print_section(title, database.get_calibration_summary(conn, since_iso=since_iso))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
