import os

import requests
from dotenv import load_dotenv

load_dotenv()

ALPHA_VANTAGE_URL = "https://www.alphavantage.co/query"
MARKETAUX_URL = "https://api.marketaux.com/v1/news/all"

_LAST_STATE = {}


def _set_state(provider, status, count=0):
    _LAST_STATE[provider] = {"status": status, "count": count}


def _placeholder(value):
    return not value or value.strip() in ("", "PLACEHOLDER")


def fetch_alphavantage_news():
    key = (os.getenv("ALPHA_VANTAGE_KEY") or "").strip()
    if _placeholder(key):
        _set_state("alpha_vantage", "sem key", 0)
        return []

    try:
        response = requests.get(
            ALPHA_VANTAGE_URL,
            params={
                "function": "NEWS_SENTIMENT",
                "topics": "economy_fiscal,economy_monetary,forex",
                "sort": "LATEST",
                "limit": "20",
                "apikey": key,
            },
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()

        if "Information" in data or "Note" in data:
            _set_state("alpha_vantage", "limite atingido", 0)
            return []

        feed = data.get("feed") or []
        articles = []
        for item in feed:
            score = item.get("overall_sentiment_score")
            try:
                score = float(score) if score is not None else None
            except (TypeError, ValueError):
                score = None

            articles.append({
                "title": item.get("title", ""),
                "summary": item.get("summary", ""),
                "source": item.get("source", "Alpha Vantage"),
                "published": item.get("time_published", ""),
                "sentiment_score": score,
            })

        _set_state("alpha_vantage", "ok", len(articles))
        return articles

    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        print(f"[api_sources] Alpha Vantage HTTP {code}: {e}")
        _set_state("alpha_vantage", "erro", 0)
        return []
    except Exception as e:
        print(f"[api_sources] Alpha Vantage erro: {e}")
        _set_state("alpha_vantage", "erro", 0)
        return []


def fetch_marketaux_news():
    key = (os.getenv("MARKETAUX_KEY") or "").strip()
    if _placeholder(key):
        _set_state("marketaux", "sem key", 0)
        return []

    try:
        response = requests.get(
            MARKETAUX_URL,
            params={
                "symbols": "EURUSD",
                "filter_entities": "true",
                "language": "en",
                "api_token": key,
            },
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
        items = data.get("data") or []

        articles = []
        for item in items:
            articles.append({
                "title": item.get("title", ""),
                "summary": item.get("description") or item.get("snippet") or "",
                "source": item.get("source", "Marketaux"),
                "published": item.get("published_at", ""),
            })

        _set_state("marketaux", "ok", len(articles))
        return articles

    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        print(f"[api_sources] Marketaux HTTP {code}: {e}")
        _set_state("marketaux", "erro", 0)
        return []
    except Exception as e:
        print(f"[api_sources] Marketaux erro: {e}")
        _set_state("marketaux", "erro", 0)
        return []


def get_status():
    if "alpha_vantage" not in _LAST_STATE:
        fetch_alphavantage_news()
    if "marketaux" not in _LAST_STATE:
        fetch_marketaux_news()
    return {
        "alpha_vantage": _LAST_STATE.get("alpha_vantage", {}).get("status", "erro"),
        "marketaux": _LAST_STATE.get("marketaux", {}).get("status", "erro"),
    }


def get_state():
    return dict(_LAST_STATE)
