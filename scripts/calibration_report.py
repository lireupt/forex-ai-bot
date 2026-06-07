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


def _print_metrics_block(label, metrics, indent="  "):
    print(f"{label}")
    print(f"{indent}trades: {metrics['trades']}")
    print(f"{indent}wins: {metrics['wins']}")
    print(f"{indent}losses: {metrics['losses']}")
    print(f"{indent}expired: {metrics['expired']}")
    print(f"{indent}winrate: {_fmt(metrics['winrate'], '%')}")
    print(f"{indent}avg_pips: {_fmt(metrics['avg_pips'])}")
    print(f"{indent}net_pips: {_fmt(metrics['net_pips'])}")
    print(f"{indent}profit_factor: {_fmt(metrics['profit_factor'])}")
    print(f"{indent}expectancy: {_fmt(metrics['expectancy'])}")


def _print_aggregator_section(analysis):
    print("\n=== AI AGGREGATOR ANALYSIS ===")
    if not analysis.get("available"):
        print(f"(indisponível: {analysis.get('reason')})")
        print(f"recommendation: {analysis['recommendation']}")
        return

    print(f"window: {analysis['window']} | total_evaluated: {analysis['total_evaluated']}")
    _print_metrics_block("should_trade=True", analysis["should_trade_true"])
    _print_metrics_block("should_trade=False", analysis["should_trade_false"])

    impact = analysis["impact_if_veto_enabled"]
    print("impact_if_veto_enabled:")
    print(f"  winrate_change: {_fmt(impact['winrate_change'], '%')}")
    print(f"  net_pips_change: {_fmt(impact['net_pips_change'])}")
    print(f"  expectancy_change: {_fmt(impact['expectancy_change'])}")
    print(f"  profit_factor_change: {_fmt(impact['profit_factor_change'])}")

    agreement = analysis["agreement"]
    print("agreement:")
    print(f"  agree: {agreement['agree']} ({_fmt(agreement['agreement_rate'], '%')})")
    print(f"  disagree: {agreement['disagree']}")
    print(f"  winrate_when_agree: {_fmt(agreement['winrate_when_agree'], '%')}")
    print(f"  winrate_when_disagree: {_fmt(agreement['winrate_when_disagree'], '%')}")

    print("risk_level:")
    for level, bucket in analysis["risk_level"].items():
        print(
            f"  {level}: trades={bucket['trades']} wins={bucket['wins']} "
            f"losses={bucket['losses']} net_pips={_fmt(bucket['net_pips'])}"
        )

    if analysis["warnings"]:
        print("warnings:")
        for warning, count in analysis["warnings"].items():
            print(f"  - {warning}: {count}")
    else:
        print("warnings: -")

    print(f"\nrecommendation: {analysis['recommendation']}")
    for reason in analysis.get("recommendation_reasons", []):
        print(f"  - {reason}")


def _print_rolling_context_section(conn):
    print("\n=== ROLLING MARKET CONTEXT ===")
    try:
        ctx = database.get_latest_rolling_market_context(conn, "EUR/USD")
    except Exception as e:
        print(f"(indisponível: {type(e).__name__}: {e})")
        return

    if not ctx:
        print("(sem contexto guardado ainda)")
        return

    print(f"created_at:          {ctx.get('created_at', '-')}")
    print(f"market_phase:        {_fmt(ctx.get('market_phase'))}")
    print(f"macro_bias:          {_fmt(ctx.get('macro_bias'))}")
    print(f"technical_bias:      {_fmt(ctx.get('technical_bias'))}")
    print(f"combined_bias:       {_fmt(ctx.get('combined_bias'))}")
    print(f"confidence:          {_fmt(ctx.get('confidence'), '%')}")
    print(f"risk_level:          {_fmt(ctx.get('risk_level'))}")
    print(f"recommended_stance:  {_fmt(ctx.get('recommended_stance'))}")
    print(f"likely_market_intent: {_fmt(ctx.get('likely_market_intent'))}")
    print(f"short_summary:       {_fmt(ctx.get('short_summary'))}")
    key_risks = ctx.get("key_risks") or []
    if key_risks:
        print("key_risks:")
        for risk in key_risks:
            print(f"  - {risk}")
    else:
        print("key_risks: -")


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
        _print_aggregator_section(database.get_aggregator_analysis(conn, since_iso=_since(days=7)))
        _print_rolling_context_section(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
