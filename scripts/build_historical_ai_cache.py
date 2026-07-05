"""Constrói o cache diário de análise de IA histórica (Fase B).

Chama `ai_analyst.analyse()` **uma vez por dia** (não por candle) — usando
o snapshot técnico point-in-time (via `backtest_runner.HistoricalProvider`,
que já corta estritamente antes de `t`) e as notícias/eventos realmente
disponíveis nesse dia — e grava o resultado em `ai_analyses`, a mesma
tabela/mecanismo de cache que o live já usa (`database.get_ai_analysis` /
`save_ai_analysis`), chaveado por (pair, analysis_date, input_hash,
provider).

Retomável: usa um ficheiro de estado (`data/historical_ai_cache_state.json`,
fora do repo) com o último dia processado, para retomar sem re-verificar
dias já feitos. A tabela `ai_analyses` continua a ser a fonte de verdade —
se o ficheiro de estado se perder, dias já cacheados não geram chamadas
novas (dedup por input_hash).

Tem orçamento de tokens/dia (Groq free tier: 100k/dia) e pára quando o
estima esgotado — corre-se uma vez por dia até completar o intervalo. Se
uma chamada falhar (ex.: quota real esgotada, mesmo com margem — o
consumo real observado em produção é ~3300 tokens/chamada, não os 1500
estimados inicialmente), o dia falhado **não avança no ficheiro de
estado** — a próxima corrida tenta-o de novo, em vez de o saltar
silenciosamente para sempre.

Se correres isto no mesmo servidor/conta onde o `main.py` ao vivo já
consome a mesma quota Groq, define `GROQ_API_KEY_HISTORICAL` no `.env`
com uma chave *diferente* — este script troca-a para `GROQ_API_KEY` só
no seu próprio processo (nunca mexe em `modules/ai_analyst.py`, nem
afecta o processo separado do live), evitando que a Fase B e o ciclo
live disputem a mesma quota diária.

Uso:
    python scripts/build_historical_ai_cache.py --pair EUR/USD --from 2023-01-01 --to 2025-12-31
"""

import argparse
import hashlib
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402
load_dotenv()

if os.getenv("GROQ_API_KEY_HISTORICAL"):
    os.environ["GROQ_API_KEY"] = os.environ["GROQ_API_KEY_HISTORICAL"]

import backtest_runner as br  # noqa: E402
from modules import database  # noqa: E402
from modules.ai_analyst import analyse as analyse_ai, build_analysis_input  # noqa: E402
from modules.decision_engine import aggregate_multi_timeframe_technical  # noqa: E402
from modules.macro_filter import get_macro_risk  # noqa: E402
from modules.pair_spec import get_pair_spec  # noqa: E402

DEFAULT_TOKEN_BUDGET = 90000  # margem abaixo do limite Groq free tier (100k/dia)
ESTIMATED_TOKENS_PER_CALL = 3500  # consumo real observado em produção (~3300/chamada, ver [ai-tokens] nos logs live)
NEWS_LOOKBACK_HOURS = 72
DEFAULT_STATE_PATH = ROOT / "data" / "historical_ai_cache_state.json"


def _news_for_day(conn, pair, day_start, lookback_hours=NEWS_LOOKBACK_HOURS):
    since = (day_start - timedelta(hours=lookback_hours)).isoformat()
    until = day_start.isoformat()
    rows = conn.execute(
        """
        SELECT title, summary, url, source, published_at
        FROM news_items
        WHERE pair = ? AND published_at >= ? AND published_at < ?
        ORDER BY published_at ASC
        """,
        (pair, since, until),
    ).fetchall()
    return [dict(row) for row in rows]


def build_day_analysis(conn, provider_name, pair, day_start, candle_provider):
    """Devolve (result, input_hash, was_cached) para um dia — reaproveita
    `ai_analyses` como cache; só chama a IA se ainda não estiver lá."""
    provider = br.HistoricalProvider(conn, pair, candle_provider=candle_provider)
    candles_by_tf = {role: provider.candles_up_to(tf, day_start) for role, tf in br.TIMEFRAMES.items()}
    technical_result, _warnings = aggregate_multi_timeframe_technical(candles_by_tf, pair)

    news = _news_for_day(conn, pair, day_start)
    events = provider.macro_events()
    macro_result = get_macro_risk(pair, day_start, events=events)

    input_text = build_analysis_input(
        news, events, pair,
        technical=technical_result,
        macro_context_snapshot=macro_result["macro_context_snapshot"],
    )
    input_hash = hashlib.sha256(input_text.encode("utf-8")).hexdigest()
    analysis_date = day_start.date().isoformat()

    cached = database.get_ai_analysis(conn, pair, analysis_date, input_hash, provider_name)
    if cached:
        return cached, input_hash, True

    result = analyse_ai(
        news, events, pair,
        technical=technical_result,
        macro_context_snapshot=macro_result["macro_context_snapshot"],
    )
    if result.get("status") != "failed":
        database.save_ai_analysis(conn, pair, analysis_date, input_hash, result)
    return result, input_hash, False


def _load_state(state_path):
    if state_path.exists():
        import json
        return json.loads(state_path.read_text())
    return {"last_day": None}


def _save_state(state_path, state):
    import json
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2))


def build_historical_ai_cache(
    pair, date_from, date_to,
    provider_name=None, candle_provider="import",
    token_budget=DEFAULT_TOKEN_BUDGET, db_path=None,
    state_path=DEFAULT_STATE_PATH,
):
    provider_name = provider_name or "groq"

    if db_path:
        database.DB_PATH = Path(db_path)
    conn = database.connect()
    database.init_db(conn)

    state = _load_state(state_path)
    last_day = state.get("last_day")
    day = datetime.fromisoformat(last_day) + timedelta(days=1) if last_day else date_from
    if day < date_from:
        day = date_from

    tokens_used = 0
    calls_made = 0
    cached_hits = 0
    days_processed = 0
    failed = 0

    while day < date_to:
        if tokens_used + ESTIMATED_TOKENS_PER_CALL > token_budget:
            break

        result, _input_hash, was_cached = build_day_analysis(conn, provider_name, pair, day, candle_provider)
        if result.get("status") == "failed":
            failed += 1
            break  # não avança o estado — a próxima corrida tenta este dia de novo

        if was_cached:
            cached_hits += 1
        else:
            calls_made += 1
            tokens_used += ESTIMATED_TOKENS_PER_CALL

        days_processed += 1
        state["last_day"] = day.isoformat()
        _save_state(state_path, state)
        day += timedelta(days=1)

    conn.close()

    total_days = (date_to - date_from).days
    days_done = (day - date_from).days if day <= date_to else total_days
    return {
        "days_processed": days_processed,
        "calls_made": calls_made,
        "cached_hits": cached_hits,
        "failed": failed,
        "total_days": total_days,
        "days_remaining": max(0, total_days - days_done),
    }


def _to_utc(date_str):
    dt = datetime.fromisoformat(date_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def main():
    parser = argparse.ArgumentParser(description="Constrói o cache diário de IA histórica (Fase B).")
    parser.add_argument("--pair", default="EUR/USD")
    parser.add_argument("--from", dest="date_from", required=True)
    parser.add_argument("--to", dest="date_to", required=True)
    parser.add_argument("--provider", default=None)
    parser.add_argument("--candle-provider", default="import")
    parser.add_argument("--token-budget", type=int, default=DEFAULT_TOKEN_BUDGET)
    parser.add_argument("--state-file", default=str(DEFAULT_STATE_PATH))
    parser.add_argument("--db", default=None)
    args = parser.parse_args()

    stats = build_historical_ai_cache(
        args.pair, _to_utc(args.date_from), _to_utc(args.date_to),
        provider_name=args.provider, candle_provider=args.candle_provider,
        token_budget=args.token_budget, db_path=args.db,
        state_path=Path(args.state_file),
    )
    print(
        f"[build_historical_ai_cache] {stats['days_processed']} dias processados "
        f"({stats['calls_made']} chamadas novas, {stats['cached_hits']} já em cache, "
        f"{stats['failed']} falhas)."
    )
    if stats["days_remaining"] > 0:
        print(f"[build_historical_ai_cache] {stats['days_remaining']} dias por fazer — corre de novo amanhã.")
    else:
        print("[build_historical_ai_cache] concluído — todos os dias processados.")


if __name__ == "__main__":
    main()
