"""Teste de equivalência: compara o backtest_runner (Passo 5) com as trades
reais gravadas em `paper_trades` durante o live, para o mesmo período.

Nível (a) — com os `ai_score`/`ai_confidence_score`/etc. reais das decisões
live injetados no `MarketContext` via `ai_result_provider`: as trades
geradas pelo backtest devem coincidir com as reais em direcção, entry, SL,
TP e resultado (tolerância: 0.5 pip). Se não baterem, é bug no motor de
decisão (`modules.decision_engine`) — corrigir antes de fechar a Fase A.

Nível (b) — com `ai_result=None` (comportamento normal da Fase A, sem IA):
reporta as diferenças, sem exigir igualdade — é esperado divergir, já que
a IA influencia o sinal ao vivo.

Requer um snapshot local da DB de produção (nunca liga ao servidor —
copiar via `sqlite3 '.backup'` no VPS e trazer o ficheiro para local).

Uso:
    python scripts/backtest_equivalence.py --pair EUR/USD --from 2026-06-30 --to 2026-07-15 \
        --db /caminho/para/snapshot_producao.db
"""

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import backtest_runner as br  # noqa: E402
from modules import database  # noqa: E402
from modules.pair_spec import get_pair_spec  # noqa: E402

PIP_TOLERANCE = 0.5
MATCH_WINDOW_MINUTES = 10


def _parse_dt(value):
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def build_ai_result_lookup(conn, pair, date_from, date_to, window_minutes=MATCH_WINDOW_MINUTES):
    """candle_time_iso -> ai_result, reconstruído a partir das decisões live
    gravadas em `decisions` — replay do ai_score real (nível a).

    `ai_confidence` bruto (0-100) não é gravado hoje (ver Passo 8); é
    reconstruído a partir de `ai_confidence_score` (unit [0,1]) já gravado.
    """
    rows = conn.execute(
        """
        SELECT timestamp, ai_signal, ai_bias, ai_confidence_score,
               ai_confidence_adjustment, ai_risk_adjustment, hold_off, ai_status
        FROM decisions
        WHERE pair = ? AND timestamp >= ? AND timestamp <= ?
        ORDER BY timestamp ASC
        """,
        (pair, date_from, date_to),
    ).fetchall()

    entries = []
    for row in rows:
        d = dict(row)
        conf_score = d.get("ai_confidence_score") or 0.0
        ai_result = {
            "signal": d.get("ai_signal") or "NEUTRAL",
            "bias": d.get("ai_bias") or d.get("ai_signal") or "NEUTRAL",
            "confidence": round(conf_score * 100),
            "confidence_adjustment": d.get("ai_confidence_adjustment") or 0.0,
            "risk_adjustment": d.get("ai_risk_adjustment") or 0.0,
            "hold_off": bool(d.get("hold_off")),
            "status": d.get("ai_status") or "ok",
            "reasoning": "",
            "reason": "",
            "macro_context": "",
            "volatility_context": "",
            "news_sentiment": "",
        }
        entries.append((_parse_dt(d["timestamp"]), ai_result))

    def lookup(candle_time_iso):
        target = _parse_dt(candle_time_iso)
        best, best_delta = None, None
        for ts, ai_result in entries:
            delta = abs((ts - target).total_seconds())
            if delta > window_minutes * 60:
                continue
            if best_delta is None or delta < best_delta:
                best, best_delta = ai_result, delta
        return best

    return lookup


def _pip_diff(a, b, pip_size):
    if a is None or b is None:
        return None
    return round(abs(a - b) / pip_size, 2)


def compare_trades(backtest_trades, live_trades, pip_size,
                   tolerance_pips=PIP_TOLERANCE, window_minutes=MATCH_WINDOW_MINUTES):
    """Emparelha cada trade do backtest com a trade live mais próxima no
    tempo (mesma direcção, dentro de `window_minutes`) e compara
    entry/SL/TP/resultado."""
    matches = []
    unmatched_backtest = []
    remaining_live = list(live_trades)

    for bt in backtest_trades:
        bt_time = _parse_dt(bt["created_at"])
        candidate, candidate_delta = None, None
        for lt in remaining_live:
            if lt.get("direction") != bt["direction"]:
                continue
            delta = abs((_parse_dt(lt["created_at"]) - bt_time).total_seconds())
            if delta > window_minutes * 60:
                continue
            if candidate_delta is None or delta < candidate_delta:
                candidate, candidate_delta = lt, delta
        if candidate is None:
            unmatched_backtest.append(bt)
            continue
        remaining_live.remove(candidate)

        entry_diff = _pip_diff(bt["entry_price"], candidate["entry_price"], pip_size)
        sl_diff = _pip_diff(bt["simulated_sl"], candidate["simulated_sl"], pip_size)
        tp_diff = _pip_diff(bt["simulated_tp"], candidate["simulated_tp"], pip_size)
        result_match = bt["status"] == candidate.get("status")
        within_tolerance = (
            result_match
            and entry_diff is not None and entry_diff <= tolerance_pips
            and sl_diff is not None and sl_diff <= tolerance_pips
            and tp_diff is not None and tp_diff <= tolerance_pips
        )
        matches.append({
            "backtest_trade": bt,
            "live_trade": candidate,
            "entry_diff_pips": entry_diff,
            "sl_diff_pips": sl_diff,
            "tp_diff_pips": tp_diff,
            "result_match": result_match,
            "within_tolerance": within_tolerance,
        })

    return {"matched": matches, "unmatched_backtest": unmatched_backtest, "unmatched_live": remaining_live}


def run_equivalence(pair, date_from, date_to, db_path=None, level="a"):
    if db_path:
        database.DB_PATH = Path(db_path)
    pair_spec = get_pair_spec(pair)

    conn = database.connect()
    live_trades = database.get_paper_trades(conn, limit=10000, status=None, source="combined")
    live_trades = [
        t for t in live_trades
        if t.get("pair") == pair and date_from <= t["created_at"] <= date_to
    ]
    ai_provider = build_ai_result_lookup(conn, pair, date_from, date_to) if level == "a" else None
    conn.close()

    stats = br.run_backtest(pair, date_from, date_to, db_path=db_path, ai_result_provider=ai_provider)

    conn = database.connect()
    backtest_trades = database.get_backtest_trades(conn, stats["run_id"])
    conn.close()

    comparison = compare_trades(backtest_trades, live_trades, pair_spec.pip_size)
    return stats, comparison


def _print_report(level, stats, comparison):
    total_backtest = len(comparison["matched"]) + len(comparison["unmatched_backtest"])
    total_live = len(comparison["matched"]) + len(comparison["unmatched_live"])
    mismatches = [m for m in comparison["matched"] if not m["within_tolerance"]]

    print(f"=== Equivalência (nível {level}) — run_id={stats['run_id']} ===")
    print(f"Backtest: {total_backtest} trades | Live: {total_live} trades")
    print(f"Emparelhadas: {len(comparison['matched'])}")
    print(f"Dentro da tolerância (0.5 pip, mesmo resultado): {len(comparison['matched']) - len(mismatches)}")
    print(f"Fora da tolerância: {len(mismatches)}")
    print(f"Só no backtest (sem par no live): {len(comparison['unmatched_backtest'])}")
    print(f"Só no live (sem par no backtest): {len(comparison['unmatched_live'])}")

    if mismatches:
        print("\nDiscrepâncias:")
        for m in mismatches[:20]:
            bt, lt = m["backtest_trade"], m["live_trade"]
            print(
                f"  {bt['created_at']} {bt['direction']}: "
                f"entry_diff={m['entry_diff_pips']}pip sl_diff={m['sl_diff_pips']}pip "
                f"tp_diff={m['tp_diff_pips']}pip "
                f"resultado backtest={bt['status']} vs live={lt.get('status')}"
            )

    if level == "a":
        ok = (
            not mismatches
            and not comparison["unmatched_backtest"]
            and not comparison["unmatched_live"]
        )
        print(f"\n[nível a] {'PASSOU' if ok else 'FALHOU — bug no motor, corrigir antes de fechar a Fase A.'}")
        return ok
    return True


def main():
    parser = argparse.ArgumentParser(description="Teste de equivalência backtest vs live.")
    parser.add_argument("--pair", default="EUR/USD")
    parser.add_argument("--from", dest="date_from", required=True)
    parser.add_argument("--to", dest="date_to", required=True)
    parser.add_argument("--db", default=None, help="Snapshot local da DB de produção (nunca liga ao servidor).")
    parser.add_argument("--level", choices=["a", "b", "both"], default="both")
    args = parser.parse_args()

    date_from = br.to_utc_iso(args.date_from)
    date_to = br.to_utc_iso(args.date_to)

    ok = True
    if args.level in ("a", "both"):
        stats_a, comparison_a = run_equivalence(args.pair, date_from, date_to, db_path=args.db, level="a")
        ok = _print_report("a", stats_a, comparison_a)
        print()
    if args.level in ("b", "both"):
        stats_b, comparison_b = run_equivalence(args.pair, date_from, date_to, db_path=args.db, level="b")
        _print_report("b", stats_b, comparison_b)

    if args.level in ("a", "both") and not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
