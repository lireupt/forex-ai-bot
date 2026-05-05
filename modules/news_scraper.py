import feedparser
import requests
from bs4 import BeautifulSoup

from modules.api_sources import (
    fetch_alphavantage_news,
    fetch_marketaux_news,
    get_state as _api_state,
)

RSS_FEEDS = {
    "MarketWatch (Dow Jones)": "https://feeds.content.dowjones.io/public/rss/mw_realtimeheadlines",
    "Yahoo Finance Top": "https://finance.yahoo.com/rss/topfinstories",
    "Yahoo Finance EURUSD": "https://finance.yahoo.com/rss/2.0/headline?s=EURUSD=X&region=US&lang=en-US",
    "ForexLive": "https://www.forexlive.com/feed/news",
    "BBC Business": "http://feeds.bbci.co.uk/news/business/rss.xml",
    "CNBC": "https://www.cnbc.com/id/20910258/device/rss/rss.html",
    "Investing.com Forex (PT)": "https://pt.investing.com/rss/news_25.rss",
}

FEEDPARSER_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept-Language": "pt-PT,pt;q=0.9",
}

EURUSD_KEYWORDS = ["EUR", "USD", "ECB", "Fed", "euro", "dollar"]

EVENT_RSS_FEEDS = [
    "https://www.investing.com/rss/news_288.rss",
    "https://www.forexlive.com/feed/news",
]

EVENT_KEYWORDS = [
    "CPI",
    "NFP",
    "GDP",
    "interest rate",
    "Fed",
    "ECB",
    "inflation",
    "employment",
    "central bank",
    "rate decision",
]

CURRENCY_CODES = ("USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD", "CNY")

SCRAPER_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept-Language": "pt-PT,pt;q=0.9",
}

FXSTREET_CALENDAR_URL = "https://pt.fxstreet.com/economic-calendar"
INVESTING_NEWS_URL = "https://pt.investing.com/news/forex-news"

_SCRAPE_STATUS = {}


def _set_status(name, status, count=0):
    _SCRAPE_STATUS[name] = {"status": status, "count": count}


def fetch_rss_news(with_sources=False):
    articles = []
    per_feed = {}
    for source, url in RSS_FEEDS.items():
        count = 0
        try:
            feed = feedparser.parse(url, request_headers=FEEDPARSER_HEADERS)
            for entry in feed.entries:
                articles.append({
                    "source": source,
                    "title": entry.get("title", ""),
                    "summary": entry.get("summary", ""),
                    "link": entry.get("link", ""),
                    "published": entry.get("published", ""),
                })
                count += 1
        except Exception as e:
            print(f"[news_scraper] Erro ao ler {source}: {e}")
        per_feed[source] = count

    if with_sources:
        return articles, per_feed
    return articles


def filter_forex_relevant(articles, keywords=None):
    if keywords is None:
        keywords = EURUSD_KEYWORDS
    keywords_lower = [k.lower() for k in keywords]

    relevant = []
    for art in articles:
        haystack = f"{art.get('title', '')} {art.get('summary', '')}".lower()
        if any(kw in haystack for kw in keywords_lower):
            relevant.append(art)
    return relevant


def _detect_currency(text):
    upper = text.upper()
    for code in CURRENCY_CODES:
        if code in upper:
            return code
    lowered = text.lower()
    if "fed" in lowered or "u.s." in lowered or "united states" in lowered:
        return "USD"
    if "ecb" in lowered or "eurozone" in lowered or "euro area" in lowered:
        return "EUR"
    if "boe" in lowered or "bank of england" in lowered:
        return "GBP"
    if "boj" in lowered or "bank of japan" in lowered:
        return "JPY"
    return ""


def fetch_forex_factory_events():
    events = []
    keywords_lower = [k.lower() for k in EVENT_KEYWORDS]
    seen_titles = set()

    for url in EVENT_RSS_FEEDS:
        try:
            feed = feedparser.parse(url, request_headers=FEEDPARSER_HEADERS)
            for entry in feed.entries:
                title = entry.get("title", "").strip()
                if not title:
                    continue

                summary = entry.get("summary", "")
                haystack = f"{title} {summary}".lower()
                if not any(kw in haystack for kw in keywords_lower):
                    continue

                if title in seen_titles:
                    continue
                seen_titles.add(title)

                events.append({
                    "currency": _detect_currency(f"{title} {summary}"),
                    "event": title,
                    "time": entry.get("published", ""),
                    "impact": "high",
                })
        except Exception as e:
            print(f"[news_scraper] Erro ao ler {url}: {e}")
    return events


def _is_high_impact(row_html):
    blob = row_html.lower()
    if "high" in blob or "alto" in blob:
        return True
    bull_count = blob.count("bull") + blob.count("touro")
    return bull_count >= 3


def fetch_fxstreet_calendar():
    events = []
    try:
        response = requests.get(
            FXSTREET_CALENDAR_URL,
            headers=SCRAPER_HEADERS,
            timeout=15,
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        rows = soup.select("tr") or soup.select("[data-event], .fxs_calendar_row")
        for row in rows:
            row_html = str(row)
            if not _is_high_impact(row_html):
                continue

            cells = row.find_all(["td", "div", "span"], recursive=True)
            if not cells:
                continue

            currency = ""
            event = ""
            time = ""

            for cell in cells:
                text = cell.get_text(strip=True)
                if not text:
                    continue
                if not currency and text in CURRENCY_CODES:
                    currency = text
                if not time and ":" in text and len(text) <= 8:
                    time = text
                if not event and 10 < len(text) < 200:
                    event = text

            if event:
                events.append({
                    "currency": currency or _detect_currency(event),
                    "event": event,
                    "time": time,
                    "impact": "high",
                })

        if events:
            _set_status("fxstreet", "ok", len(events))
        else:
            _set_status("fxstreet", "blocked_js", 0)
        return events

    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        print(f"[news_scraper] FX Street bloqueado ({code}): {e}")
        _set_status("fxstreet", f"blocked_{code}", 0)
        return []
    except Exception as e:
        print(f"[news_scraper] Erro ao ler FX Street: {e}")
        _set_status("fxstreet", "erro", 0)
        return []


def fetch_investing_news():
    articles = []
    try:
        response = requests.get(
            INVESTING_NEWS_URL,
            headers=SCRAPER_HEADERS,
            timeout=15,
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        items = (
            soup.select("article")
            or soup.select(".articleItem, .js-article-item, [data-test='article-item']")
        )

        for item in items:
            title_el = item.select_one("a[title], h2 a, h3 a, .title a, a.title")
            if not title_el:
                title_el = item.find("a")
            if not title_el:
                continue

            title = (title_el.get("title") or title_el.get_text(strip=True) or "").strip()
            if not title:
                continue

            link = title_el.get("href", "")
            if link and link.startswith("/"):
                link = f"https://pt.investing.com{link}"

            summary_el = item.select_one("p, .articleDetails, .description")
            summary = summary_el.get_text(strip=True) if summary_el else ""

            time_el = item.select_one("time, .date, .articleDetails span")
            published = ""
            if time_el:
                published = time_el.get("datetime") or time_el.get_text(strip=True)

            articles.append({
                "title": title,
                "summary": summary,
                "source": "Investing.com (scrape)",
                "published": published,
                "link": link,
            })

        _set_status("investing_html", "ok" if articles else "blocked_js", len(articles))
        return articles

    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        print(f"[news_scraper] Investing.com bloqueado ({code}): {e}")
        _set_status("investing_html", f"blocked_{code}", 0)
        return []
    except Exception as e:
        print(f"[news_scraper] Erro ao ler Investing.com: {e}")
        _set_status("investing_html", "erro", 0)
        return []


def _dedup_by(items, key_field):
    seen = set()
    unique = []
    for item in items:
        key = item.get(key_field, "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def fetch_all_events(with_sources=False):
    rss_economic = fetch_forex_factory_events()
    fxstreet = fetch_fxstreet_calendar()
    unique = _dedup_by(rss_economic + fxstreet, "event")
    unique.sort(key=lambda e: e.get("currency") or "ZZZ")

    if with_sources:
        return unique, {
            "rss_economic": len(rss_economic),
            "fxstreet": _SCRAPE_STATUS.get(
                "fxstreet", {"status": "ok", "count": len(fxstreet)}
            ),
        }
    return unique


def fetch_all_news(with_sources=False):
    rss_articles, rss_per_feed = fetch_rss_news(with_sources=True)
    investing = fetch_investing_news()
    av = fetch_alphavantage_news()
    mx = fetch_marketaux_news()

    combined = rss_articles + investing + av + mx
    relevant = filter_forex_relevant(_dedup_by(combined, "title"))

    if with_sources:
        api_state = _api_state()
        sources = {
            "rss_per_feed": rss_per_feed,
            "investing_html": _SCRAPE_STATUS.get(
                "investing_html", {"status": "ok", "count": len(investing)}
            ),
            "alphavantage": api_state.get(
                "alpha_vantage", {"status": "ok", "count": len(av)}
            ),
            "marketaux": api_state.get(
                "marketaux", {"status": "ok", "count": len(mx)}
            ),
        }
        return relevant, sources
    return relevant
