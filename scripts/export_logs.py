"""Exporta as últimas decisões para um JSON estático consumido pelo dashboard.

- Lê SQLite primeiro (data/forex_bot.db), faz fallback para logs/decisions.jsonl.
- Aplica whitelist de campos (não expõe API keys, .env, raw logs ou DB completa).
- Idempotente e fail-safe: nunca levanta excepções para fora.

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
DB_PATH = ROOT / "data" / "forex_bot.db"
JSONL_PATH = ROOT / "logs" / "decisions.jsonl"
DEFAULT_OUT = ROOT / "web" / "data.json"
DEFAULT_LIMIT = 50

EXPORT_FIELDS = (
    "timestamp",
    "pair",
    "timeframe",
    "ai_signal",
    "technical_signal",
    "shadow_technical_signal",
    "combined_signal",
    "confidence",
    "trade_allowed",
    "block_reason",
    "current_price",
    "atr_pips",
    "volatility_level",
    "dangerous_event_nearby",
    "dangerous_event_reason",
)


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


def _normalise(row):
    atr_pips = row.get("atr_pips")
    return {
        "timestamp": row.get("timestamp") or "",
        "pair": row.get("pair") or "",
        "timeframe": row.get("timeframe") or "",
        "ai_signal": row.get("ai_signal") or "NEUTRAL",
        "technical_signal": row.get("technical_signal") or "NEUTRAL",
        "shadow_technical_signal": row.get("shadow_technical_signal") or "NEUTRAL",
        "combined_signal": row.get("combined_signal") or "NEUTRAL",
        "confidence": int(row.get("confidence") or 0),
        "trade_allowed": _coerce_bool(row.get("trade_allowed")),
        "block_reason": row.get("block_reason") or "",
        "current_price": row.get("current_price"),
        "atr_pips": atr_pips,
        "volatility_level": _volatility_level(atr_pips),
        "dangerous_event_nearby": _coerce_bool(row.get("dangerous_event_nearby")),
        "dangerous_event_reason": row.get("dangerous_event_reason") or "",
    }


def _read_from_sqlite(limit):
    if not DB_PATH.exists():
        return None, "db ausente"
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT timestamp, pair, timeframe, ai_signal, technical_signal,
                       shadow_technical_signal, combined_signal, confidence,
                       trade_allowed, block_reason, current_price, atr_pips,
                       dangerous_event_nearby, dangerous_event_reason
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

    items = [_normalise(dict(row)) for row in rows]
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
    allowed = 0
    blocked = 0
    confidence_sum = 0

    for item in items:
        sig = item["combined_signal"] if item["combined_signal"] in counts else "NEUTRAL"
        counts[sig] += 1
        shadow = item["shadow_technical_signal"]
        if shadow not in shadow_counts:
            shadow = "NEUTRAL"
        shadow_counts[shadow] += 1

        if item["trade_allowed"]:
            allowed += 1
        else:
            blocked += 1
        confidence_sum += item["confidence"]

    total = len(items)
    avg_conf = round(confidence_sum / total, 1) if total else 0

    return {
        "total": total,
        "buy": counts["BUY"],
        "sell": counts["SELL"],
        "neutral": counts["NEUTRAL"],
        "shadow_buy": shadow_counts["BUY"],
        "shadow_sell": shadow_counts["SELL"],
        "shadow_neutral": shadow_counts["NEUTRAL"],
        "allowed": allowed,
        "blocked": blocked,
        "average_confidence": avg_conf,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def export(out_path=DEFAULT_OUT, limit=DEFAULT_LIMIT):
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    items, source = _read_from_sqlite(limit)
    if items is None:
        items, source = _read_from_jsonl(limit)

    items = items or []
    summary = _summarise(items)
    summary["source"] = source

    payload = {
        "summary": summary,
        "decisions": items,
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
