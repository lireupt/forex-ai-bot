"""Importador de notícias históricas via Alpha Vantage `NEWS_SENTIMENT`,
para `news_items` — usado pela Fase B (replay de IA histórico).

O tier gratuito do Alpha Vantage tem limite de 25 pedidos/dia **por
chave**. Este importador corre em blocos mensais e é **retomável**:
guarda o progresso num ficheiro de estado JSON e, em cada execução, só
pede os meses ainda não descarregados, até ao orçamento diário
configurado. Corre-se uma vez por dia até `months_remaining` chegar a 0.

Suporta uma segunda chave via `ALPHA_VANTAGE_KEY_2` — os pedidos
alternam entre as chaves disponíveis (round-robin), duplicando o
orçamento diário efectivo (2 × 20 = 40 pedidos/dia, com margem).

`tickers=FOREX:EUR` não devolve dados históricos de forma fiável no tier
gratuito (testado); usa-se antes `topics` (macro/monetário/fiscal/
mercados financeiros) com filtragem de relevância EUR/USD do lado do
cliente, reaproveitando `news_scraper.EURUSD_KEYWORDS`.

Uso:
    python scripts/import_historical_news.py --pair EUR/USD --from 2023-01-01 --to 2025-12-31
    # corre-se de novo, uma vez por dia, até completar:
    python scripts/import_historical_news.py --pair EUR/USD --from 2023-01-01 --to 2025-12-31
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv()

from modules import database  # noqa: E402
from modules.news_scraper import EURUSD_KEYWORDS  # noqa: E402

API_URL = "https://www.alphavantage.co/query"
TOPICS = "economy_macro,economy_monetary,economy_fiscal,financial_markets"
PER_KEY_DAILY_BUDGET = 20  # margem abaixo do limite grátis de 25/dia por chave
REQUEST_INTERVAL_SECONDS = 15
DEFAULT_STATE_PATH = ROOT / "data" / "historical_news_import_state.json"


def _load_state(state_path):
    if state_path.exists():
        return json.loads(state_path.read_text())
    return {"done": []}


def _save_state(state_path, state):
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2))


def _monthly_windows(date_from, date_to):
    windows = []
    cursor = date_from.replace(day=1)
    while cursor < date_to:
        next_month = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
        window_end = min(next_month, date_to)
        windows.append((cursor, window_end))
        cursor = next_month
    return windows


def _parse_av_time(value):
    """"20230106T215200" -> ISO 8601 UTC."""
    dt = datetime.strptime(value, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _is_relevant(article):
    text = f"{article.get('title', '')} {article.get('summary', '')}".upper()
    return any(kw.upper() in text for kw in EURUSD_KEYWORDS)


class RateLimitError(RuntimeError):
    """A chave atingiu a quota diária — não é um erro fatal, é sinal para
    tentar outra chave ou parar graciosamente por hoje."""


def _fetch_window(api_key, time_from, time_to, limit=1000):
    params = {
        "function": "NEWS_SENTIMENT",
        "topics": TOPICS,
        "time_from": time_from,
        "time_to": time_to,
        "sort": "EARLIEST",
        "limit": limit,
        "apikey": api_key,
    }
    response = requests.get(API_URL, params=params, timeout=30)
    response.raise_for_status()
    data = response.json()
    if "Information" in data:
        message = data["Information"]
        if "rate limit" in message.lower() or "requests per day" in message.lower():
            raise RateLimitError(message)
        raise RuntimeError(f"Alpha Vantage: {message}")
    if "feed" not in data:
        raise RuntimeError(f"Alpha Vantage não devolveu 'feed': {data}")
    return data["feed"]


def _fetch_with_rotation(api_keys, exhausted_keys, key_cursor, time_from, time_to):
    """Tenta obter a janela, rodando para a chave seguinte disponível sempre
    que uma bate no limite diário. Devolve (feed, novo_key_cursor); `feed` é
    `None` se todas as chaves estiverem esgotadas (chamador deve parar sem
    marcar o mês como concluído)."""
    for _ in range(len(api_keys)):
        key_index = key_cursor % len(api_keys)
        if key_index not in exhausted_keys:
            try:
                feed = _fetch_window(api_keys[key_index], time_from, time_to)
                return feed, key_cursor + 1
            except RateLimitError:
                exhausted_keys.add(key_index)
        key_cursor += 1
    return None, key_cursor


def _article_to_news_item(article):
    published = article.get("time_published")
    return {
        "title": article.get("title", ""),
        "summary": article.get("summary", ""),
        "url": article.get("url", ""),
        "source": article.get("source", ""),
        "published_at": _parse_av_time(published) if published else "",
    }


def import_historical_news(
    pair, date_from, date_to, api_keys,
    state_path=DEFAULT_STATE_PATH, daily_budget=None,
    sleep_seconds=REQUEST_INTERVAL_SECONDS, db_path=None,
):
    """`api_keys` aceita uma única chave (string) ou uma lista — com mais
    de uma, os pedidos alternam entre elas (round-robin) e o orçamento
    diário por omissão escala com o número de chaves
    (`PER_KEY_DAILY_BUDGET` × len(api_keys))."""
    if isinstance(api_keys, str):
        api_keys = [api_keys]
    if daily_budget is None:
        daily_budget = PER_KEY_DAILY_BUDGET * len(api_keys)

    if db_path:
        database.DB_PATH = Path(db_path)

    state = _load_state(state_path)
    windows = _monthly_windows(date_from, date_to)

    conn = database.connect()
    database.init_db(conn)

    requests_made = 0
    imported_total = 0
    exhausted_keys = set()
    key_cursor = 0
    for start, end in windows:
        month_key = start.strftime("%Y-%m")
        if month_key in state.get("done", []):
            continue
        if requests_made >= daily_budget:
            break
        if len(exhausted_keys) >= len(api_keys):
            break  # todas as chaves esgotaram a quota diária — pára graciosamente

        feed, key_cursor = _fetch_with_rotation(
            api_keys, exhausted_keys, key_cursor,
            start.strftime("%Y%m%dT%H%M"), end.strftime("%Y%m%dT%H%M"),
        )
        if feed is None:
            break  # todas as chaves esgotadas — o mês não é marcado como feito

        requests_made += 1
        relevant = [a for a in feed if _is_relevant(a)]
        items = [_article_to_news_item(a) for a in relevant]
        database.save_news_items(conn, items, pair)
        imported_total += len(items)

        state.setdefault("done", []).append(month_key)
        _save_state(state_path, state)

        if requests_made < daily_budget and sleep_seconds:
            time.sleep(sleep_seconds)

    conn.close()

    done_count = len(state.get("done", []))
    return {
        "requests_made": requests_made,
        "imported": imported_total,
        "months_done": done_count,
        "months_total": len(windows),
        "months_remaining": len(windows) - done_count,
        "keys_exhausted": len(exhausted_keys),
    }


def _to_utc(date_str):
    dt = datetime.fromisoformat(date_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _load_api_keys():
    keys = []
    for env_name in ("ALPHA_VANTAGE_KEY", "ALPHA_VANTAGE_KEY_2"):
        value = os.getenv(env_name)
        if value and value != "PLACEHOLDER":
            keys.append(value)
    return keys


def main():
    parser = argparse.ArgumentParser(description="Importa notícias históricas via Alpha Vantage.")
    parser.add_argument("--pair", default="EUR/USD")
    parser.add_argument("--from", dest="date_from", required=True)
    parser.add_argument("--to", dest="date_to", required=True)
    parser.add_argument("--state-file", default=str(DEFAULT_STATE_PATH))
    parser.add_argument("--daily-budget", type=int, default=None)
    parser.add_argument("--db", default=None, help="SQLite alternativo (isolar do forex_bot.db de produção).")
    args = parser.parse_args()

    api_keys = _load_api_keys()
    if not api_keys:
        print("[import_historical_news] ALPHA_VANTAGE_KEY não configurada.", file=sys.stderr)
        sys.exit(1)
    if len(api_keys) > 1:
        print(f"[import_historical_news] {len(api_keys)} chaves Alpha Vantage detectadas — a alternar entre elas.")

    stats = import_historical_news(
        args.pair, _to_utc(args.date_from), _to_utc(args.date_to), api_keys,
        state_path=Path(args.state_file), daily_budget=args.daily_budget, db_path=args.db,
    )
    print(
        f"[import_historical_news] {stats['requests_made']} pedidos, "
        f"{stats['imported']} notícias relevantes importadas."
    )
    print(f"[import_historical_news] {stats['months_done']}/{stats['months_total']} meses concluídos.")
    if stats["keys_exhausted"] > 0:
        print(
            f"[import_historical_news] {stats['keys_exhausted']}/{len(api_keys)} chave(s) "
            "atingiram o limite diário — parou graciosamente, progresso guardado."
        )
    if stats["months_remaining"] > 0:
        print(
            f"[import_historical_news] {stats['months_remaining']} meses por fazer — "
            "corre de novo amanhã (quota diária do Alpha Vantage)."
        )
    else:
        print("[import_historical_news] concluído — todos os meses importados.")


if __name__ == "__main__":
    main()
