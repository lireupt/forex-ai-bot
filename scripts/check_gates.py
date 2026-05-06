"""Avalia se a estratégia cumpre os critérios mínimos para mudar de fase.

Critérios (configuráveis por env):

    GATE_MIN_TRADES          (default 50)
    GATE_MIN_PROFIT_FACTOR   (default 1.3)
    GATE_MIN_AVG_R           (default 0.2)
    GATE_MIN_WIN_RATE        (default 38)   # %
    GATE_MAX_STREAK_LOSSES   (default 5)
    GATE_MAX_DRAWDOWN_PCT    (default 15)   # %, equity peak-to-valley

Estados globais:
    go      — todos os gates passam
    no_go   — pelo menos um gate falha (com amostra suficiente)
    partial — não há trades suficientes para concluir

Output:
    - data/gates_check.json (snapshot último, sobrescrito)
    - tabela gate_checks na DB (histórico)
    - stdout legível (a menos que --quiet)

Uso:
    python scripts/check_gates.py
    python scripts/check_gates.py --quiet
    python scripts/check_gates.py --history 10
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules import database  # noqa: E402

GATES_OUT = ROOT / "data" / "gates_check.json"

DEFAULT_MIN_TRADES = 50
DEFAULT_MIN_PROFIT_FACTOR = 1.3
DEFAULT_MIN_AVG_R = 0.2
DEFAULT_MIN_WIN_RATE = 38.0
DEFAULT_MAX_STREAK_LOSSES = 5
DEFAULT_MAX_DRAWDOWN_PCT = 15.0


def _env_float(name, default):
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return default
    try:
        return float(v)
    except ValueError:
        return default


def _env_int(name, default):
    return int(_env_float(name, default))


def load_gate_config():
    return {
        "min_trades": _env_int("GATE_MIN_TRADES", DEFAULT_MIN_TRADES),
        "min_profit_factor": _env_float("GATE_MIN_PROFIT_FACTOR", DEFAULT_MIN_PROFIT_FACTOR),
        "min_avg_r": _env_float("GATE_MIN_AVG_R", DEFAULT_MIN_AVG_R),
        "min_win_rate": _env_float("GATE_MIN_WIN_RATE", DEFAULT_MIN_WIN_RATE),
        "max_streak_losses": _env_int("GATE_MAX_STREAK_LOSSES", DEFAULT_MAX_STREAK_LOSSES),
        "max_drawdown_pct": _env_float("GATE_MAX_DRAWDOWN_PCT", DEFAULT_MAX_DRAWDOWN_PCT),
    }


def _filter_closed(trades):
    """Trades que já resolveram (não-open)."""
    return [t for t in trades if t.get("status") in ("win", "loss", "expired")]


def _filter_by(trades, source=None, direction=None):
    out = trades
    if source:
        out = [t for t in out if t.get("source") == source]
    if direction:
        out = [t for t in out if t.get("direction") == direction]
    return out


def profit_factor(trades):
    wins = [t["result_pips"] for t in trades
            if t.get("result_pips") is not None and t["result_pips"] > 0]
    losses = [-t["result_pips"] for t in trades
              if t.get("result_pips") is not None and t["result_pips"] < 0]
    if not losses:
        if not wins:
            return None
        # Sem losses mas com wins -> infinito; cap para apresentação
        return 999.0
    return round(sum(wins) / sum(losses), 2)


def average_r(trades):
    rs = [t["result_r_multiple"] for t in trades
          if t.get("result_r_multiple") is not None]
    if not rs:
        return None
    return round(sum(rs) / len(rs), 2)


def win_rate(trades):
    """Win rate sobre trades resolvidas (win+loss). Ignora 'expired'."""
    decisive = [t for t in trades if t.get("status") in ("win", "loss")]
    if not decisive:
        return None
    wins = sum(1 for t in decisive if t["status"] == "win")
    return round(wins / len(decisive) * 100, 1)


def max_losing_streak(trades):
    """Maior sequência de losses consecutivos por ordem cronológica de fecho."""
    closed = sorted(
        [t for t in trades if t.get("status") in ("win", "loss") and t.get("closed_at")],
        key=lambda t: t["closed_at"],
    )
    max_streak = 0
    current = 0
    for t in closed:
        if t["status"] == "loss":
            current += 1
            if current > max_streak:
                max_streak = current
        else:
            current = 0
    return max_streak


def max_drawdown_pct(trades, risk_per_trade_pct=1.0):
    """Drawdown peak-to-valley em % equity, assumindo `risk_per_trade_pct`
    por trade (default 1%). Soma R-multiple a equity = 100%, mede pior queda."""
    closed = sorted(
        [t for t in trades
         if t.get("result_r_multiple") is not None and t.get("closed_at")],
        key=lambda t: t["closed_at"],
    )
    if not closed:
        return None
    equity = 100.0
    peak = equity
    max_dd = 0.0
    for t in closed:
        equity += float(t["result_r_multiple"]) * risk_per_trade_pct
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak * 100 if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
    return round(max_dd, 2)


def compute_metrics(trades, risk_per_trade_pct=1.0):
    closed = _filter_closed(trades)
    return {
        "total": len(closed),
        "wins": sum(1 for t in closed if t["status"] == "win"),
        "losses": sum(1 for t in closed if t["status"] == "loss"),
        "expired": sum(1 for t in closed if t["status"] == "expired"),
        "open": sum(1 for t in trades if t.get("status") == "open"),
        "win_rate": win_rate(closed),
        "profit_factor": profit_factor(closed),
        "avg_r": average_r(closed),
        "max_losing_streak": max_losing_streak(closed),
        "max_drawdown_pct": max_drawdown_pct(closed, risk_per_trade_pct),
    }


def _gate(name, label, value, threshold, comparator, value_unit=""):
    """Cria a estrutura comum de um gate."""
    if value is None:
        return {
            "name": name, "label": label, "value": None,
            "threshold": threshold, "pass": False, "insufficient_data": True,
            "value_unit": value_unit,
        }
    return {
        "name": name, "label": label, "value": value,
        "threshold": threshold, "pass": comparator(value, threshold),
        "insufficient_data": False, "value_unit": value_unit,
    }


def evaluate_gates(metrics, config):
    closed_total = (metrics["wins"] or 0) + (metrics["losses"] or 0) + (metrics["expired"] or 0)

    gates = [
        _gate(
            "min_trades",
            f"Trades fechados ≥ {config['min_trades']}",
            closed_total, config["min_trades"], lambda v, t: v >= t,
        ),
        _gate(
            "profit_factor",
            f"Profit factor ≥ {config['min_profit_factor']}",
            metrics["profit_factor"], config["min_profit_factor"],
            lambda v, t: v >= t,
        ),
        _gate(
            "avg_r",
            f"Avg R ≥ {config['min_avg_r']}",
            metrics["avg_r"], config["min_avg_r"],
            lambda v, t: v >= t, value_unit="R",
        ),
        _gate(
            "win_rate",
            f"Win rate ≥ {config['min_win_rate']}%",
            metrics["win_rate"], config["min_win_rate"],
            lambda v, t: v >= t, value_unit="%",
        ),
        _gate(
            "max_streak_losses",
            f"Max streak losses ≤ {config['max_streak_losses']}",
            metrics["max_losing_streak"], config["max_streak_losses"],
            lambda v, t: v <= t,
        ),
        _gate(
            "max_drawdown",
            f"Max drawdown ≤ {config['max_drawdown_pct']}%",
            metrics["max_drawdown_pct"], config["max_drawdown_pct"],
            lambda v, t: v <= t, value_unit="%",
        ),
    ]

    if closed_total < config["min_trades"]:
        status = "partial"  # não há amostra suficiente para concluir
    elif all(g["pass"] for g in gates):
        status = "go"
    else:
        status = "no_go"

    return status, gates


def _evaluate_split(closed, *, source=None, direction=None,
                    config=None, risk_per_trade_pct=1.0):
    subset = _filter_by(closed, source=source, direction=direction)
    metrics = compute_metrics(subset, risk_per_trade_pct=risk_per_trade_pct)
    status, gates = evaluate_gates(metrics, config)
    return {"metrics": metrics, "status": status, "gates": gates}


def run_check(conn=None, persist=True):
    """Corre o check e devolve o snapshot. Se `conn` for None, abre uma nova
    e fecha; caso contrário usa a fornecida (útil em testes)."""
    own_conn = conn is None
    if own_conn:
        conn = database.connect()
        database.init_db(conn)

    try:
        risk_per_trade_pct = _env_float("RISK_PER_TRADE_PERCENT", 1.0)
        trades = database.get_paper_trades(conn, limit=10000)
        closed = _filter_closed(trades)
        config = load_gate_config()

        overall_metrics = compute_metrics(trades, risk_per_trade_pct)
        overall_status, overall_gates = evaluate_gates(overall_metrics, config)

        by_source = {
            source: _evaluate_split(
                closed, source=source, config=config,
                risk_per_trade_pct=risk_per_trade_pct,
            )
            for source in ("ai_only", "combined")
        }
        by_direction = {
            direction: _evaluate_split(
                closed, direction=direction, config=config,
                risk_per_trade_pct=risk_per_trade_pct,
            )
            for direction in ("BUY", "SELL")
        }

        snapshot = {
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "config": config,
            "risk_per_trade_pct": risk_per_trade_pct,
            "overall": {
                "metrics": overall_metrics,
                "status": overall_status,
                "gates": overall_gates,
            },
            "by_source": by_source,
            "by_direction": by_direction,
        }

        if persist:
            GATES_OUT.parent.mkdir(parents=True, exist_ok=True)
            tmp = GATES_OUT.with_suffix(GATES_OUT.suffix + ".tmp")
            tmp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2))
            tmp.replace(GATES_OUT)
            database.save_gate_check(conn, snapshot)

        return snapshot
    finally:
        if own_conn:
            conn.close()


_STATUS_ICONS = {"go": "[GO]", "partial": "[PARTIAL]", "no_go": "[NO-GO]"}


def _print_status(snapshot):
    overall = snapshot["overall"]
    status = overall["status"]
    metrics = overall["metrics"]
    closed_total = metrics["wins"] + metrics["losses"] + metrics["expired"]

    print(f"=== Validation gates  {_STATUS_ICONS.get(status, status)} ===")
    print(f"Checked at: {snapshot['checked_at']}")
    print()
    print("Métricas globais:")
    print(f"  trades fechados: {closed_total}  (W:{metrics['wins']} / L:{metrics['losses']} / E:{metrics['expired']})  (open: {metrics['open']})")
    print(f"  WR (W/(W+L)):    {metrics['win_rate']}%")
    print(f"  PF:              {metrics['profit_factor']}")
    print(f"  avg R:           {metrics['avg_r']}")
    print(f"  max streak loss: {metrics['max_losing_streak']}")
    print(f"  max drawdown:    {metrics['max_drawdown_pct']}%")
    print()
    print("Gates:")
    for g in overall["gates"]:
        if g.get("insufficient_data"):
            mark = "?"
        else:
            mark = "v" if g["pass"] else "x"
        unit = g.get("value_unit") or ""
        value = "n/a" if g["value"] is None else f"{g['value']}{unit}"
        print(f"  [{mark}] {g['label']:42}  valor: {value}")
    print()
    print("Por fonte:")
    for src, data in snapshot["by_source"].items():
        m = data["metrics"]
        n = m["wins"] + m["losses"] + m["expired"]
        print(f"  {src:10}  {_STATUS_ICONS.get(data['status'], data['status']):10}  n={n}  WR={m['win_rate']}%  PF={m['profit_factor']}  R={m['avg_r']}")
    print()
    print("Por direcção:")
    for d, data in snapshot["by_direction"].items():
        m = data["metrics"]
        n = m["wins"] + m["losses"] + m["expired"]
        print(f"  {d:10}  {_STATUS_ICONS.get(data['status'], data['status']):10}  n={n}  WR={m['win_rate']}%  PF={m['profit_factor']}  R={m['avg_r']}")


def _print_history(conn, limit):
    rows = database.get_recent_gate_checks(conn, limit=limit)
    if not rows:
        print("Sem histórico de gate checks ainda.")
        return
    print("=== Histórico ===")
    for row in rows:
        print(
            f"{row['checked_at']:30}  {_STATUS_ICONS.get(row['status'], row['status']):10}  "
            f"trades={row['total_trades']}  WR={row['win_rate']}%  PF={row['profit_factor']}  R={row['avg_r']}"
        )


def main():
    parser = argparse.ArgumentParser(description="Avalia gates de validação.")
    parser.add_argument("--quiet", action="store_true",
                        help="Não imprime nada para stdout (apenas escreve snapshot).")
    parser.add_argument("--history", type=int, default=0,
                        help="Imprime últimos N gate checks anteriores além do actual.")
    args = parser.parse_args()

    snapshot = run_check()

    if not args.quiet:
        _print_status(snapshot)

    if args.history > 0:
        conn = database.connect()
        try:
            print()
            _print_history(conn, args.history)
        finally:
            conn.close()


if __name__ == "__main__":
    main()
