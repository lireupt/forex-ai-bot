"""Métricas de performance sobre paper trades e decisões — sem I/O.

Extraído de `modules/database.calculate_analytics_metrics` para ser
reutilizável pelo motor de decisão (`modules/decision_engine.py`), que
precisa da mesma matemática sobre listas já filtradas point-in-time, sem
aceder à DB.
"""


def max_drawdown(values):
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return round(max_dd, 4)


def sharpe(values):
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    if variance <= 0:
        return None
    return round(mean / (variance ** 0.5), 4)


def compute_metrics(trades, decisions):
    """`trades` e `decisions` já vêm filtrados/ordenados (mais recente
    primeiro) e truncados ao `limit` desejado pelo chamador — replica
    exactamente a matemática das duas queries SQL de
    `database.calculate_analytics_metrics`."""
    r_values = [float(t["result_r_multiple"]) for t in trades if t.get("result_r_multiple") is not None]
    wins = [v for v in r_values if v > 0]
    losses = [v for v in r_values if v < 0]
    total = len(r_values)
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))

    scores = [float(d["combined_score"]) for d in decisions if d.get("combined_score") is not None]
    ai_impacts = [abs(float(d["ai_score"])) for d in decisions if d.get("ai_score") is not None]
    h4d1 = []
    aligned_allowed = 0
    aligned_total = 0
    for d in decisions:
        if d.get("technical_score_h4") is not None and d.get("technical_score_d1") is not None:
            h4d1.append(abs(float(d["technical_score_h4"])) + abs(float(d["technical_score_d1"])))
        alignment = d.get("timeframe_alignment") or ""
        if "aligned" in alignment:
            aligned_total += 1
            if d.get("trade_allowed"):
                aligned_allowed += 1

    return {
        "trade_count": total,
        "winrate": round(len(wins) / total * 100, 2) if total else None,
        "average_rr": round(sum(r_values) / total, 4) if total else None,
        "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss else (None if not gross_profit else gross_profit),
        "expectancy": round(sum(r_values) / total, 4) if total else None,
        "max_drawdown": max_drawdown(list(reversed(r_values))) if total else None,
        "sharpe_ratio": sharpe(r_values),
        "average_score": round(sum(scores) / len(scores), 4) if scores else None,
        "ai_impact": round(sum(ai_impacts) / len(ai_impacts), 4) if ai_impacts else None,
        "h4_d1_impact": round(sum(h4d1) / len(h4d1), 4) if h4d1 else None,
        "alignment_success_rate": round(aligned_allowed / aligned_total * 100, 2) if aligned_total else None,
    }
