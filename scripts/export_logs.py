"""Exporta as últimas decisões para um JSON estático consumido pelo dashboard.

- Lê SQLite primeiro (data/forex_bot.db), faz fallback para logs/decisions.jsonl.
- Aplica whitelist de campos (não expõe API keys, .env, raw logs ou DB completa).
- Inclui scores, explicações estruturadas e estado de paper-trades por decisão.
- Idempotente e fail-safe: nunca levanta excepções para fora.
- para adiconar
Uso:
    python scripts/export_logs.py
    python scripts/export_logs.py --limit 100 --out web/data.json
"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DB_PATH = ROOT / "data" / "forex_bot.db"
JSONL_PATH = ROOT / "logs" / "decisions.jsonl"
GATES_PATH = ROOT / "data" / "gates_check.json"
DEFAULT_OUT = ROOT / "web" / "data.json"
DEFAULT_LIMIT = 50

from modules import database  # noqa: E402


def _volatility_level(atr_pips):
    if atr_pips is None:
        return "unknown"
    try:
        value = float(atr_pips)
    except (TypeError, ValueError):
        return "unknown"
    if value < 8:
        return "low"
    if value <= 20:
        return "normal"
    return "high"


def _coerce_bool(value):
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y")
    return bool(value)


def _coerce_float(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_features_snapshot(value):
    if value is None or value == "":
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {}
    return {}


def _parse_json_obj(value):
    if value is None or value == "":
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _parse_json_list(value):
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _first_text(row, *keys):
    for key in keys:
        value = row.get(key)
        if value:
            return str(value)
    return ""


def _model_version(row):
    explicit = row.get("ai_model_version") or row.get("model_version") or row.get("model")
    if explicit:
        return str(explicit)
    provider = row.get("provider") or row.get("ai_provider")
    if provider:
        return str(provider)
    return "unknown"


AI_ANALYSIS_UNAVAILABLE = "Análise IA não disponível para esta decisão."


def _split_sentences(text):
    if not text:
        return []
    sentences = []
    current = []
    for char in str(text).strip():
        current.append(char)
        if char in ".!?":
            sentence = "".join(current).strip()
            if sentence:
                sentences.append(sentence)
            current = []
    tail = "".join(current).strip()
    if tail:
        sentences.append(tail)
    return sentences


def _ai_reason(row):
    text = _first_text(row, "ai_reason", "reasoning", "reason", "explanation", "analysis")
    sentences = _split_sentences(text)
    if sentences:
        return " ".join(sentences[:2])
    return text


def _ai_analysis_text(row):
    text = _first_text(
        row,
        "ai_analysis_text",
        "ai_reason",
        "reasoning",
        "reason",
        "explanation",
        "analysis",
    )
    return text or AI_ANALYSIS_UNAVAILABLE


def _adaptive_risk(gate_diagnostics):
    """Extrai os campos da AdaptiveRiskEngine para o topo do item exportado.

    Para decisões NEUTRAL (bloqueadas antes do gate de execução) não existe
    `adaptive_risk` nos diagnostics; devolvemos um objeto vazio coerente.
    """
    adaptive = (gate_diagnostics or {}).get("adaptive_risk") or {}
    return {
        "allow_trade": adaptive.get("allow_trade"),
        "adaptive_min_confidence": adaptive.get("adaptive_min_confidence"),
        "effective_confidence": adaptive.get("effective_confidence"),
        "raw_confidence": adaptive.get("raw_confidence"),
        "score_strength": adaptive.get("score_strength"),
        "risk_multiplier": adaptive.get("risk_multiplier"),
        "dynamic_exposure": adaptive.get("dynamic_exposure"),
        "execution_reason": adaptive.get("execution_reason"),
        "block_reason": adaptive.get("block_reason"),
        "bonuses": adaptive.get("bonuses") or [],
        "penalties": adaptive.get("penalties") or [],
        "context_blocks": adaptive.get("context_blocks") or [],
    }


def _normalise(row, paper_trade_lookup=None):
    atr_pips = row.get("atr_pips")
    paper_trade = None
    paper_trade_id = row.get("paper_trade_id")
    if paper_trade_id and paper_trade_lookup:
        paper_trade = paper_trade_lookup.get(paper_trade_id)

    gate_diagnostics = _parse_json_obj(row.get("gate_diagnostics_json") or row.get("gate_diagnostics"))

    _agg = dict(row.get("ai_aggregated") or {})
    for _k in (
        "ai_aggregated_signal", "ai_aggregated_confidence", "ai_aggregated_score",
        "ai_aggregated_risk_level", "ai_aggregated_should_trade",
        "ai_aggregated_should_reduce_risk", "ai_aggregated_reasoning",
        "ai_aggregated_supporting_factors", "ai_aggregated_contradicting_factors",
        "ai_aggregated_warnings", "ai_aggregated_status", "ai_aggregated_model_version",
    ):
        if row.get(_k) is not None:
            _agg[_k] = row.get(_k)

    return {
        "timestamp": row.get("timestamp") or "",
        "pair": row.get("pair") or "",
        "timeframe": row.get("timeframe") or "",
        "ai_signal": row.get("ai_signal") or "NEUTRAL",
        "technical_signal": row.get("technical_signal") or "NEUTRAL",
        "shadow_technical_signal": row.get("shadow_technical_signal") or "NEUTRAL",
        "combined_signal": row.get("combined_signal") or "NEUTRAL",
        "score_combined_signal": row.get("score_combined_signal") or None,
        "gating_mode": row.get("gating_mode") or "strict",
        "gating_signal": row.get("gating_signal") or row.get("combined_signal") or "NEUTRAL",
        "gating_confidence": int(row.get("gating_confidence") or row.get("confidence") or 0),
        "confidence": int(row.get("confidence") or 0),
        "trade_allowed": _coerce_bool(row.get("trade_allowed")),
        "block_reason": row.get("block_reason") or "",
        "blocking_reason": row.get("blocking_reason") or row.get("block_reason") or "",
        "current_price": row.get("current_price"),
        "atr_pips": atr_pips,
        "atr_price": row.get("atr_price"),
        "volatility_level": _volatility_level(atr_pips),
        "dangerous_event_nearby": _coerce_bool(row.get("dangerous_event_nearby")),
        "dangerous_event_reason": row.get("dangerous_event_reason") or "",
        "gate_diagnostics": gate_diagnostics,
        "adaptive_risk": _adaptive_risk(gate_diagnostics),
        "ai_status": row.get("ai_status") or "ok",
        "neutral_reason": row.get("neutral_reason") or "",
        "ai_score": _coerce_float(row.get("ai_score")),
        "ai_confidence_score": _coerce_float(row.get("ai_confidence_score")),
        "ai_analysis_text": _ai_analysis_text(row),
        "ai_reason": _ai_reason(row),
        "ai_features_snapshot": _parse_features_snapshot(row.get("ai_features_snapshot")),
        "ai_model_version": _model_version(row),
        "ai_bias": row.get("ai_bias") or row.get("ai_signal") or "NEUTRAL",
        "ai_confidence_adjustment": _coerce_float(row.get("ai_confidence_adjustment")),
        "ai_risk_adjustment": _coerce_float(row.get("ai_risk_adjustment")),
        "macro_context": row.get("macro_context") or "",
        "volatility_context": row.get("volatility_context") or "",
        "news_sentiment": row.get("news_sentiment") or "",
        "ai_context_reason": row.get("ai_context_reason") or "",
        "technical_score": _coerce_float(row.get("technical_score")),
        "technical_score_m15": _coerce_float(row.get("technical_score_m15")),
        "technical_score_h1": _coerce_float(row.get("technical_score_h1")),
        "technical_score_h4": _coerce_float(row.get("technical_score_h4")),
        "technical_score_d1": _coerce_float(row.get("technical_score_d1")),
        "multi_timeframe_score": _coerce_float(row.get("multi_timeframe_score")),
        "timeframe_alignment": row.get("timeframe_alignment") or "",
        "timeframe_block_reason": row.get("timeframe_block_reason") or "",
        "technical_reason": row.get("technical_reason") or "",
        "shadow_score": _coerce_float(row.get("shadow_score")),
        "shadow_technical_reason": row.get("shadow_technical_reason") or "",
        "shadow_technical_confidence": row.get("shadow_technical_confidence"),
        "shadow_combined_signal": row.get("shadow_combined_signal") or "NEUTRAL",
        "shadow_combined_confidence": row.get("shadow_combined_confidence"),
        "shadow_combined_reason": row.get("shadow_combined_reason") or "",
        "combined_score": _coerce_float(row.get("combined_score")),
        "combined_reason": row.get("combined_reason") or "",
        "rsi_value": row.get("rsi_value"),
        "ema20_value": row.get("ema20_value"),
        "ema50_value": row.get("ema50_value"),
        "macd_value": row.get("macd_value"),
        "macd_signal_value": row.get("macd_signal_value"),
        "adx_value": row.get("adx_value"),
        "rsi_vote": row.get("rsi_vote") or "neutral",
        "ema_vote": row.get("ema_vote") or "neutral",
        "macd_vote": row.get("macd_vote") or "neutral",
        "paper_trade": paper_trade,
        "operational_mode": row.get("operational_mode") or "",
        "operational_can_trade": _coerce_bool(row.get("operational_can_trade")),
        "operational_block_reason": row.get("operational_block_reason") or "",
        "ai_aggregated_signal": _agg.get("ai_aggregated_signal") or None,
        "ai_aggregated_confidence": _agg.get("ai_aggregated_confidence"),
        "ai_aggregated_score": _coerce_float(_agg.get("ai_aggregated_score")),
        "ai_aggregated_risk_level": _agg.get("ai_aggregated_risk_level") or _agg.get("risk_level") or "",
        "ai_aggregated_should_trade": _coerce_bool(_agg.get("ai_aggregated_should_trade", _agg.get("should_trade"))),
        "ai_aggregated_should_reduce_risk": _coerce_bool(_agg.get("ai_aggregated_should_reduce_risk", _agg.get("should_reduce_risk"))),
        "ai_aggregated_reasoning": _agg.get("ai_aggregated_reasoning") or _agg.get("reasoning_summary") or "",
        "ai_aggregated_supporting_factors": _parse_json_list(_agg.get("ai_aggregated_supporting_factors", _agg.get("supporting_factors"))),
        "ai_aggregated_contradicting_factors": _parse_json_list(_agg.get("ai_aggregated_contradicting_factors", _agg.get("contradicting_factors"))),
        "ai_aggregated_warnings": _parse_json_list(_agg.get("ai_aggregated_warnings", _agg.get("warnings"))),
        "ai_aggregated_status": _agg.get("ai_aggregated_status") or _agg.get("status") or None,
        "ai_aggregated_model_version": _agg.get("ai_aggregated_model_version") or _agg.get("model_version") or None,
    }


def _row_to_paper_trade(row):
    return {
        "id": row.get("id"),
        "decision_id": row.get("decision_id"),
        "pair": row.get("pair"),
        "timeframe": row.get("timeframe"),
        "direction": row.get("direction"),
        "entry_price": row.get("entry_price"),
        "simulated_sl": row.get("simulated_sl"),
        "simulated_tp": row.get("simulated_tp"),
        "sl_pips": row.get("sl_pips"),
        "tp_pips": row.get("tp_pips"),
        "atr_pips": row.get("atr_pips"),
        "status": row.get("status") or "open",
        "source": row.get("source") or "",
        "signal_source": row.get("signal_source") or "",
        "created_at": row.get("created_at"),
        "expiry_at": row.get("expiry_at"),
        "close_price": row.get("close_price"),
        "closed_at": row.get("closed_at"),
        "close_reason": row.get("close_reason") or "",
        "result_pips": row.get("result_pips"),
        "result_r_multiple": row.get("result_r_multiple"),
    }


def _summarise_paper_trades(trades):
    def _aggregate(filtered):
        total = len(filtered)
        wins = sum(1 for t in filtered if t["status"] == "win")
        losses = sum(1 for t in filtered if t["status"] == "loss")
        expired = sum(1 for t in filtered if t["status"] == "expired")
        open_count = sum(1 for t in filtered if t["status"] == "open")
        closed = wins + losses
        pips = [t["result_pips"] for t in filtered if t.get("result_pips") is not None]
        rs = [t["result_r_multiple"] for t in filtered if t.get("result_r_multiple") is not None]
        profit = sum(t["result_pips"] for t in filtered if t.get("result_pips") is not None and t["result_pips"] > 0)
        loss = -sum(t["result_pips"] for t in filtered if t.get("result_pips") is not None and t["result_pips"] < 0)
        loss_streak = 0
        max_loss_streak = 0
        equity = peak = 100.0
        max_dd = 0.0
        for t in sorted(filtered, key=lambda item: item.get("closed_at") or item.get("created_at") or ""):
            if t.get("status") == "loss":
                loss_streak += 1
                max_loss_streak = max(max_loss_streak, loss_streak)
            elif t.get("status") == "win":
                loss_streak = 0
            if t.get("result_r_multiple") is not None:
                equity += float(t["result_r_multiple"])
                peak = max(peak, equity)
                if peak > 0:
                    max_dd = max(max_dd, (peak - equity) / peak * 100)
        profit_factor = None
        if loss > 0:
            profit_factor = round(profit / loss, 2)
        elif profit > 0:
            profit_factor = 999.0
        status = "partial"
        if closed >= 50:
            status = "go" if (profit_factor or 0) >= 1.3 and (sum(rs) / len(rs) if rs else 0) >= 0.2 else "no_go"
        return {
            "n": total,
            "total": total,
            "open": open_count,
            "wins": wins,
            "losses": losses,
            "expired": expired,
            "win_rate": round(wins / closed * 100, 1) if closed else None,
            "profit_factor": profit_factor,
            "avg_pips": round(sum(pips) / len(pips), 1) if pips else None,
            "avg_r": round(sum(rs) / len(rs), 2) if rs else None,
            "max_drawdown": round(max_dd, 2),
            "max_loss_streak": max_loss_streak,
            "status": status,
            "best_pips": round(max(pips), 1) if pips else None,
            "worst_pips": round(min(pips), 1) if pips else None,
        }

    return {
        "all": _aggregate(trades),
        "ai_only": _aggregate([t for t in trades if t.get("source") == "ai_only"]),
        "combined": _aggregate([t for t in trades if t.get("source") == "combined"]),
        "shadow_combined": _aggregate([t for t in trades if t.get("source") == "shadow_combined"]),
        "buy": _aggregate([t for t in trades if t.get("direction") == "BUY"]),
        "sell": _aggregate([t for t in trades if t.get("direction") == "SELL"]),
    }


def _read_paper_trades_from_sqlite(limit=200):
    if not DB_PATH.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT id, decision_id, pair, timeframe, direction, entry_price,
                       simulated_sl, simulated_tp, sl_pips, tp_pips, atr_pips,
                       status, source, signal_source, created_at, expiry_at,
                       close_price, closed_at, close_reason, result_pips,
                       result_r_multiple
                FROM paper_trades
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return []
    return [_row_to_paper_trade(dict(row)) for row in rows]


def _read_from_sqlite(limit):
    if not DB_PATH.exists():
        return None, "db ausente"
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            cols = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(decisions)").fetchall()
            }
            ai_analysis_select = (
                "ai_analysis_text"
                if "ai_analysis_text" in cols
                else "NULL AS ai_analysis_text"
            )
            cols_expr = {
                "gate_diagnostics_json": "gate_diagnostics_json" if "gate_diagnostics_json" in cols else "NULL AS gate_diagnostics_json",
                "ai_status": "ai_status" if "ai_status" in cols else "NULL AS ai_status",
                "neutral_reason": "neutral_reason" if "neutral_reason" in cols else "NULL AS neutral_reason",
                "adx_value": "adx_value" if "adx_value" in cols else "NULL AS adx_value",
                "technical_score_m15": "technical_score_m15" if "technical_score_m15" in cols else "NULL AS technical_score_m15",
                "technical_score_h1": "technical_score_h1" if "technical_score_h1" in cols else "NULL AS technical_score_h1",
                "technical_score_h4": "technical_score_h4" if "technical_score_h4" in cols else "NULL AS technical_score_h4",
                "technical_score_d1": "technical_score_d1" if "technical_score_d1" in cols else "NULL AS technical_score_d1",
                "multi_timeframe_score": "multi_timeframe_score" if "multi_timeframe_score" in cols else "NULL AS multi_timeframe_score",
                "timeframe_alignment": "timeframe_alignment" if "timeframe_alignment" in cols else "NULL AS timeframe_alignment",
                "timeframe_block_reason": "timeframe_block_reason" if "timeframe_block_reason" in cols else "NULL AS timeframe_block_reason",
                "ai_bias": "ai_bias" if "ai_bias" in cols else "NULL AS ai_bias",
                "ai_confidence_adjustment": "ai_confidence_adjustment" if "ai_confidence_adjustment" in cols else "NULL AS ai_confidence_adjustment",
                "ai_risk_adjustment": "ai_risk_adjustment" if "ai_risk_adjustment" in cols else "NULL AS ai_risk_adjustment",
                "macro_context": "macro_context" if "macro_context" in cols else "NULL AS macro_context",
                "volatility_context": "volatility_context" if "volatility_context" in cols else "NULL AS volatility_context",
                "news_sentiment": "news_sentiment" if "news_sentiment" in cols else "NULL AS news_sentiment",
                "ai_context_reason": "ai_context_reason" if "ai_context_reason" in cols else "NULL AS ai_context_reason",
                "operational_mode": "operational_mode" if "operational_mode" in cols else "NULL AS operational_mode",
                "operational_can_trade": "operational_can_trade" if "operational_can_trade" in cols else "NULL AS operational_can_trade",
                "operational_block_reason": "operational_block_reason" if "operational_block_reason" in cols else "NULL AS operational_block_reason",
            }
            for _agg_col in (
                "ai_aggregated_signal", "ai_aggregated_confidence", "ai_aggregated_score",
                "ai_aggregated_risk_level", "ai_aggregated_should_trade",
                "ai_aggregated_should_reduce_risk", "ai_aggregated_reasoning",
                "ai_aggregated_supporting_factors", "ai_aggregated_contradicting_factors",
                "ai_aggregated_warnings", "ai_aggregated_status", "ai_aggregated_model_version",
            ):
                cols_expr[_agg_col] = _agg_col if _agg_col in cols else f"NULL AS {_agg_col}"
            rows = conn.execute(
                f"""
                SELECT timestamp, pair, timeframe, ai_signal, technical_signal,
                       shadow_technical_signal, shadow_combined_signal,
                       shadow_combined_confidence, shadow_combined_reason,
                       combined_signal, score_combined_signal,
                       gating_mode, gating_signal, gating_confidence, confidence,
                       trade_allowed, block_reason, blocking_reason,
                       current_price, atr_pips, atr_price,
                       dangerous_event_nearby, dangerous_event_reason,
                       {cols_expr['gate_diagnostics_json']},
                       {cols_expr['ai_status']},
                       {cols_expr['neutral_reason']},
                       ai_score, ai_confidence_score, {ai_analysis_select}, ai_reason,
                       ai_features_snapshot, ai_model_version,
                       {cols_expr['ai_bias']}, {cols_expr['ai_confidence_adjustment']},
                       {cols_expr['ai_risk_adjustment']}, {cols_expr['macro_context']},
                       {cols_expr['volatility_context']}, {cols_expr['news_sentiment']},
                       {cols_expr['ai_context_reason']},
                       technical_score, {cols_expr['technical_score_m15']},
                       {cols_expr['technical_score_h1']},
                       {cols_expr['technical_score_h4']},
                       {cols_expr['technical_score_d1']},
                       {cols_expr['multi_timeframe_score']},
                       {cols_expr['timeframe_alignment']},
                       {cols_expr['timeframe_block_reason']},
                       technical_reason, shadow_score,
                       shadow_technical_reason, shadow_technical_confidence,
                       combined_score, combined_reason,
                       rsi_value, ema20_value, ema50_value, macd_value,
                       macd_signal_value, {cols_expr['adx_value']},
                       rsi_vote, ema_vote, macd_vote,
                       paper_trade_id, {cols_expr['operational_mode']},
                       {cols_expr['operational_can_trade']},
                       {cols_expr['operational_block_reason']},
                       {cols_expr['ai_aggregated_signal']},
                       {cols_expr['ai_aggregated_confidence']},
                       {cols_expr['ai_aggregated_score']},
                       {cols_expr['ai_aggregated_risk_level']},
                       {cols_expr['ai_aggregated_should_trade']},
                       {cols_expr['ai_aggregated_should_reduce_risk']},
                       {cols_expr['ai_aggregated_reasoning']},
                       {cols_expr['ai_aggregated_supporting_factors']},
                       {cols_expr['ai_aggregated_contradicting_factors']},
                       {cols_expr['ai_aggregated_warnings']},
                       {cols_expr['ai_aggregated_status']},
                       {cols_expr['ai_aggregated_model_version']}
                FROM decisions
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as e:
        return None, f"db erro: {type(e).__name__}: {e}"

    paper_trades = _read_paper_trades_from_sqlite(limit=500)
    paper_trade_lookup = {p["id"]: p for p in paper_trades}

    items = [_normalise(dict(row), paper_trade_lookup) for row in rows]
    items.reverse()
    return items, "sqlite"


def _read_from_jsonl(limit):
    if not JSONL_PATH.exists():
        return [], "jsonl ausente"
    try:
        with JSONL_PATH.open("r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError as e:
        return [], f"jsonl erro: {type(e).__name__}: {e}"

    items = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        items.append(_normalise(data))
    return items, "jsonl"


def _summarise(items):
    counts = {"BUY": 0, "SELL": 0, "NEUTRAL": 0}
    shadow_counts = {"BUY": 0, "SELL": 0, "NEUTRAL": 0}
    score_counts = {"BUY": 0, "SELL": 0, "NEUTRAL": 0}
    allowed = 0
    blocked = 0
    confidence_sum = 0
    score_sum = 0.0
    score_seen = 0

    for item in items:
        sig = item["combined_signal"] if item["combined_signal"] in counts else "NEUTRAL"
        counts[sig] += 1
        shadow = item["shadow_technical_signal"]
        if shadow not in shadow_counts:
            shadow = "NEUTRAL"
        shadow_counts[shadow] += 1
        score_label = item.get("score_combined_signal") or "NEUTRAL"
        if score_label not in score_counts:
            score_label = "NEUTRAL"
        score_counts[score_label] += 1

        if item["trade_allowed"]:
            allowed += 1
        else:
            blocked += 1
        confidence_sum += item["confidence"]
        if item.get("combined_score") is not None:
            score_sum += float(item["combined_score"])
            score_seen += 1

    total = len(items)
    avg_conf = round(confidence_sum / total, 1) if total else 0
    avg_combined_score = round(score_sum / score_seen, 3) if score_seen else None

    return {
        "total": total,
        "buy": counts["BUY"],
        "sell": counts["SELL"],
        "neutral": counts["NEUTRAL"],
        "shadow_buy": shadow_counts["BUY"],
        "shadow_sell": shadow_counts["SELL"],
        "shadow_neutral": shadow_counts["NEUTRAL"],
        "score_buy": score_counts["BUY"],
        "score_sell": score_counts["SELL"],
        "score_neutral": score_counts["NEUTRAL"],
        "allowed": allowed,
        "blocked": blocked,
        "average_confidence": avg_conf,
        "average_combined_score": avg_combined_score,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _read_gates_snapshot():
    if not GATES_PATH.exists():
        return None
    try:
        with GATES_PATH.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _read_gate_history_from_sqlite(limit=20):
    if not DB_PATH.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT checked_at, status, total_trades, wins, losses, expired,
                       win_rate, profit_factor, avg_r, max_streak_losses,
                       max_drawdown_pct
                FROM gate_checks
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return []
    return [dict(row) for row in rows]


def _read_calibration_summary():
    if not DB_PATH.exists():
        return {}
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            return database.get_calibration_summary(conn)
        finally:
            conn.close()
    except sqlite3.Error:
        return {}


def _read_aggregator_analysis():
    if not DB_PATH.exists():
        return {}
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            return database.get_aggregator_analysis(conn)
        finally:
            conn.close()
    except sqlite3.Error:
        return {}


def export(out_path=DEFAULT_OUT, limit=DEFAULT_LIMIT):
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    items, source = _read_from_sqlite(limit)
    if items is None:
        items, source = _read_from_jsonl(limit)

    items = items or []
    summary = _summarise(items)
    summary["source"] = source

    paper_trades = _read_paper_trades_from_sqlite(limit=500) if source == "sqlite" else []
    paper_trade_summary = _summarise_paper_trades(paper_trades)
    gates_snapshot = _read_gates_snapshot()
    gates_history = _read_gate_history_from_sqlite(limit=20)
    calibration = _read_calibration_summary() if source == "sqlite" else {}
    aggregator_analysis = _read_aggregator_analysis() if source == "sqlite" else {}

    payload = {
        "summary": summary,
        "decisions": items,
        "paper_trades": paper_trades,
        "paper_trade_summary": paper_trade_summary,
        "calibration": calibration,
        "ai_aggregator_analysis": aggregator_analysis,
        "gates_check": gates_snapshot,
        "gates_history": gates_history,
    }

    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    tmp_path.replace(out_path)
    return summary


def main():
    parser = argparse.ArgumentParser(description="Exporta logs do bot para JSON estático.")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    args = parser.parse_args()

    try:
        summary = export(out_path=args.out, limit=args.limit)
    except Exception as e:
        print(f"[export_logs] falhou: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)

    print(
        f"[export_logs] {summary['total']} decisões "
        f"(source={summary['source']}) -> {args.out}"
    )


if __name__ == "__main__":
    main()
