"""
Economic calendar — Investing.com (primary) + nfs.faireconomy.media (fallback).

Fetch strategy
--------------
Primary  : Investing.com AJAX endpoint — includes actual values for released events.
           Cache: daily (UTC day boundary). Actuals accumulate throughout the week
           so we refresh once per day to pick them up.

Fallback : nfs.faireconomy.media/ff_calendar_thisweek.json — static JSON feed with
           no actual values but very reliable. Cache: weekly (ISO Mon–Sun UTC).

Cache hierarchy (two layers):
  1. In-memory dict  — survives within the same process
  2. Disk file        — data/ff_calendar_cache.json, survives cron restarts

On fetch failure:
  - Stale cache is returned with stale=True (graceful degradation).
  - Back-off for RETRY_ON_FAIL_SECONDS (1h) before retrying to avoid hammering.

Timezones
---------
All event times are stored in UTC.
Investing.com: data-event-datetime is UTC when timeZone=55 is passed.
nfs.faireconomy.media: dates carry US Eastern offset (e.g., -04:00), converted to UTC.
Portugal: JS uses timeZone='Europe/Lisbon' (WET=UTC+0 winter, WEST=UTC+1 summer).
"""

import json
import logging
import random
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Optional

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

RETRY_ON_FAIL_SECONDS = 3600  # 1h back-off after a failed live fetch

# ── Investing.com ────────────────────────────────────────────────────────────
_INV_PAGE    = "https://www.investing.com/economic-calendar/"
_INV_AJAX    = "https://www.investing.com/economic-calendar/Service/getCalendarFilteredData"
# country codes on Investing.com (confirmed by inspection):
# USD=5, EUR=17, GBP=4, JPY=35, CAD=12, AUD=6, CHF=25, NZD=26, CNY=37
# EU/ECB=72 — needed for ECB rate decisions, press conferences, Lagarde speeches
_INV_COUNTRIES = "country[]=5&country[]=17&country[]=72&country[]=4&country[]=35&country[]=12&country[]=6&country[]=25&country[]=26&country[]=37"
_INV_IMPACTS   = "importance[]=1&importance[]=2&importance[]=3"
# timeZone=55 = UTC on Investing.com
_INV_TIMEZONE  = "timeZone=55"

# ── nfs.faireconomy.media (fallback) ─────────────────────────────────────────
_NFS_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

# ── Disk cache ────────────────────────────────────────────────────────────────
_DISK_CACHE = Path(__file__).resolve().parent.parent / "data" / "ff_calendar_cache.json"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_NFS_IMPACT_MAP = {"high": "high", "medium": "medium", "low": "low", "holiday": "none"}

_mem_cache: dict = {}
_cache_lock = Lock()


# ---------------------------------------------------------------------------
# Cache validity helpers
# ---------------------------------------------------------------------------

def _monday(dt: datetime) -> date:
    return (dt - timedelta(days=dt.weekday())).date()


def _ensure_tz(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _cache_valid(disk: dict) -> bool:
    """
    Source-aware validity:
      investing.com → valid while fetched_at is today (UTC date)
      nfs            → valid while fetched_at is in the current ISO week
    """
    try:
        fetched_at = _ensure_tz(datetime.fromisoformat(disk["fetched_at"]))
    except (KeyError, ValueError):
        return False
    now = datetime.now(timezone.utc)
    source = disk.get("source", "nfs")
    if source == "investing.com":
        return fetched_at.date() == now.date()
    # nfs: weekly
    return _monday(fetched_at) == _monday(now)


# ---------------------------------------------------------------------------
# Disk cache I/O
# ---------------------------------------------------------------------------

def _load_disk_cache() -> Optional[dict]:
    try:
        if _DISK_CACHE.exists():
            data = json.loads(_DISK_CACHE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "events" in data and "fetched_at" in data:
                return data
    except Exception:
        pass
    return None


def _save_disk_cache(events: list, source: str) -> None:
    try:
        _DISK_CACHE.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "events": events,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "source": source,
        }
        _DISK_CACHE.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        logger.warning("[ff_calendar] could not write disk cache: %s", exc)


def _record_failure(disk: Optional[dict]) -> None:
    """Stamp last_failed_at so the next cron run backs off."""
    if disk is None:
        return
    try:
        disk["last_failed_at"] = datetime.now(timezone.utc).isoformat()
        _DISK_CACHE.write_text(json.dumps(disk, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _should_backoff(disk: Optional[dict]) -> tuple:
    """Returns (backoff: bool, error_msg: str)."""
    if disk is None:
        return False, ""
    last_failed = disk.get("last_failed_at")
    if not last_failed:
        return False, ""
    try:
        lf_dt = _ensure_tz(datetime.fromisoformat(last_failed))
        age = (datetime.now(timezone.utc) - lf_dt).total_seconds()
        if age < RETRY_ON_FAIL_SECONDS:
            retry_min = int((RETRY_ON_FAIL_SECONDS - age) / 60)
            return True, f"stale (retry in {retry_min}min)"
    except (ValueError, TypeError):
        pass
    return False, ""


# ---------------------------------------------------------------------------
# Investing.com scraper
# ---------------------------------------------------------------------------

def _parse_investing_html(html: str) -> list:
    """Parse Investing.com calendar HTML into the standard event schema."""
    soup = BeautifulSoup(html, "html.parser")
    events = []

    for row in soup.find_all("tr"):
        cls = row.get("class", [])

        # Skip non-event rows
        if "js-event-item" not in cls:
            continue

        # Datetime from attribute (UTC, because we pass timeZone=55)
        dt_attr = row.get("data-event-datetime", "")
        date_utc = time_utc = ""
        if dt_attr:
            try:
                dt = datetime.strptime(dt_attr, "%Y/%m/%d %H:%M:%S").replace(tzinfo=timezone.utc)
                date_utc = dt.strftime("%Y-%m-%d")
                time_utc = dt.strftime("%H:%M")
            except ValueError:
                pass

        if not date_utc:
            continue

        curr_td    = row.select_one("td.flagCur")
        event_td   = row.select_one("td.event a")
        actual_td  = row.select_one("td[id^='eventActual_']")
        forecast_td= row.select_one("td[id^='eventForecast_']")
        prev_td    = row.select_one("td[id^='eventPrevious_']")
        impact_td  = row.select_one("td.sentiment")

        if not event_td:
            continue

        currency = curr_td.text.strip() if curr_td else ""
        # Remove flag span text artefacts (some have extra spaces)
        currency = currency.strip()

        # Impact: count filled bull icons
        impact_count = len(impact_td.select("i.grayFullBullishIcon")) if impact_td else 0
        impact = {1: "low", 2: "medium", 3: "high"}.get(impact_count, "none")

        actual   = actual_td.text.strip()   if actual_td   else ""
        forecast = forecast_td.text.strip() if forecast_td else ""
        # previous may have a revised-from span; use the displayed text
        previous = prev_td.text.strip()     if prev_td     else ""

        events.append({
            "date":     date_utc,
            "time":     time_utc,
            "currency": currency,
            "impact":   impact,
            "event":    event_td.text.strip(),
            "actual":   actual,
            "forecast": forecast,
            "previous": previous,
        })

    return events


def _fetch_investing_com() -> tuple:
    """
    Scrape Investing.com economic calendar for the current ISO week.
    Returns (events_list_or_None, error_str_or_None).
    """
    now = datetime.now(timezone.utc)
    monday = now - timedelta(days=now.weekday())
    sunday = monday + timedelta(days=6)
    d_from = monday.strftime("%Y-%m-%d")
    d_to   = sunday.strftime("%Y-%m-%d")

    session = requests.Session()
    session.headers.update({**_HEADERS, "Referer": _INV_PAGE})

    try:
        # Load main page to obtain session cookies (required by AJAX endpoint)
        r0 = session.get(_INV_PAGE, timeout=15)
        r0.raise_for_status()
    except Exception as exc:
        return None, f"Investing.com page load failed: {exc}"

    # Small random delay
    time.sleep(random.uniform(0.5, 1.5))

    post_data = (
        f"{_INV_COUNTRIES}&{_INV_IMPACTS}"
        f"&dateFrom={d_from}&dateTo={d_to}"
        f"&{_INV_TIMEZONE}&timeFilter=timeRemain"
        "&currentTab=thisWeek&submitFilters=0&limit_from=0"
    )

    try:
        r = session.post(
            _INV_AJAX,
            headers={
                **_HEADERS,
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": _INV_PAGE,
                "Accept": "application/json, text/plain, */*",
            },
            data=post_data,
            timeout=30,
        )
        r.raise_for_status()
        html = r.json().get("data", "")
    except Exception as exc:
        return None, f"Investing.com AJAX failed: {exc}"

    events = _parse_investing_html(html)
    if not events:
        return None, "Investing.com returned 0 events"

    events.sort(key=lambda e: (e["date"], e["time"] or "99:99"))
    logger.info("[ff_calendar] investing.com: %d events", len(events))
    return events, None


# ---------------------------------------------------------------------------
# nfs.faireconomy.media fallback
# ---------------------------------------------------------------------------

def _parse_nfs_event(raw: dict) -> dict:
    country = raw.get("country", "")
    currency = "" if country == "All" else country
    impact = _NFS_IMPACT_MAP.get((raw.get("impact") or "").lower(), "none")

    date_str = time_str = ""
    raw_date = raw.get("date", "")
    if raw_date:
        try:
            dt_utc = datetime.fromisoformat(raw_date).astimezone(timezone.utc)
            date_str = dt_utc.strftime("%Y-%m-%d")
            time_str = dt_utc.strftime("%H:%M")
        except ValueError:
            date_str = raw_date[:10] if len(raw_date) >= 10 else raw_date

    return {
        "date":     date_str,
        "time":     time_str,
        "currency": currency,
        "impact":   impact,
        "event":    raw.get("title", ""),
        "actual":   "",   # nfs feed never includes actual values
        "forecast": raw.get("forecast", ""),
        "previous": raw.get("previous", ""),
    }


def _fetch_nfs() -> tuple:
    """Fetch from nfs.faireconomy.media. Returns (events_or_None, error_or_None)."""
    nfs_headers = {**_HEADERS, "Accept": "application/json, */*", "Referer": "https://www.forexfactory.com/"}
    last_exc: Optional[Exception] = None

    for attempt in range(3):
        delay = random.uniform(0.3, 1.0) if attempt == 0 else (2 ** attempt) + random.uniform(0.2, 0.8)
        time.sleep(delay)
        try:
            resp = requests.get(_NFS_URL, headers=nfs_headers, timeout=15)
            resp.raise_for_status()
            raw_list = resp.json()
            if not isinstance(raw_list, list):
                raise ValueError("unexpected response type")
            events = [
                _parse_nfs_event(r)
                for r in raw_list
                if r.get("country") != "All" and r.get("title")
            ]
            events.sort(key=lambda e: (e["date"], e["time"] or "99:99"))
            logger.info("[ff_calendar] nfs: %d events", len(events))
            return events, None
        except Exception as exc:
            logger.warning("[ff_calendar] nfs attempt %d failed: %s", attempt + 1, exc)
            last_exc = exc

    return None, str(last_exc)


# ---------------------------------------------------------------------------
# Main cached fetch
# ---------------------------------------------------------------------------

def _fetch_thisweek_cached() -> tuple:
    """Returns (events, cached, stale, error).

    Layer 1: in-memory (same process).
    Layer 2: disk cache — Investing.com data valid today; nfs data valid this week.
    Layer 3: live fetch — Investing.com first, nfs fallback.
    Layer 4: stale cache with stale=True.
    """
    key = "ff_thisweek"

    # 1. In-memory cache
    with _cache_lock:
        entry = _mem_cache.get(key)
    if entry and _cache_valid(entry):
        logger.info("[ff_calendar] memory cache HIT (%s)", entry.get("source", "?"))
        return entry["events"], True, False, None

    # 2. Disk cache
    disk = _load_disk_cache()
    if disk and _cache_valid(disk):
        logger.info("[ff_calendar] disk cache HIT (%s)", disk.get("source", "?"))
        with _cache_lock:
            _mem_cache[key] = disk
        return disk["events"], True, False, None

    # 3a. Back-off check (only relevant when disk has stale data)
    backoff, backoff_msg = _should_backoff(disk)
    if backoff and disk:
        logger.info("[ff_calendar] back-off active: %s", backoff_msg)
        return disk["events"], True, True, backoff_msg

    # 3b. Live fetch — try Investing.com first
    logger.info("[ff_calendar] attempting Investing.com live fetch")
    events, err = _fetch_investing_com()

    if events is None:
        logger.warning("[ff_calendar] Investing.com failed (%s), trying nfs fallback", err)
        events, err2 = _fetch_nfs()
        source = "nfs"
        if events is None:
            _record_failure(disk)
            if disk:
                logger.warning("[ff_calendar] both sources failed, returning stale cache")
                return disk["events"], True, True, err2 or err
            return [], False, False, err2 or err
    else:
        source = "investing.com"
        err = None

    _save_disk_cache(events, source)
    entry = {"events": events, "fetched_at": datetime.now(timezone.utc).isoformat(), "source": source}
    with _cache_lock:
        _mem_cache[key] = entry

    return events, False, False, None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_today() -> tuple:
    """Returns (events, cached, stale, error) filtered to today (UTC)."""
    events, cached, stale, error = _fetch_thisweek_cached()
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return [e for e in events if e["date"] == today_str], cached, stale, error


def fetch_this_week() -> tuple:
    """Returns (events, cached, stale, error) for the current week."""
    return _fetch_thisweek_cached()


def fetch_range(from_date: str, to_date: str) -> tuple:
    """Returns (events, cached, stale, error) for an arbitrary date range."""
    events, cached, stale, error = _fetch_thisweek_cached()
    return [e for e in events if from_date <= e["date"] <= to_date], cached, stale, error
