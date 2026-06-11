"""
Macro Economic Calendar Filter.

Checks upcoming/recent economic events for a currency pair and returns a risk
assessment. Used to block trades near high-impact events and reduce confidence
near medium-impact events.

Rules
-----
High impact  : block window of MACRO_HIGH_IMPACT_BLOCK_BEFORE/AFTER_MINUTES.
Medium impact: no block; apply MACRO_MEDIUM_IMPACT_CONFIDENCE_FACTOR within
               MACRO_MEDIUM_IMPACT_WINDOW_MINUTES.
Low impact   : ignored.

Always non-fatal — any exception returns an empty (non-blocking) result.
"""

import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Env helpers
# ---------------------------------------------------------------------------

def _env_bool(name, default):
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name, default):
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pair_currencies(pair):
    """Return set of upper-case currency codes from 'EUR/USD'."""
    try:
        parts = pair.replace(" ", "").upper().split("/")
        if len(parts) == 2:
            return {parts[0], parts[1]}
    except Exception:
        pass
    return set()


def _parse_event_dt(event):
    """Build a UTC datetime from event dict. Returns None if not parseable."""
    date_str = event.get("date", "")
    time_str = event.get("time", "")

    # Standard ff_calendar format: separate date + time fields
    if date_str and time_str:
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(f"{date_str} {time_str}", fmt)
                return dt.replace(tzinfo=timezone.utc)
            except ValueError:
                continue

    # Fallback: time field contains a full ISO/RFC datetime
    if time_str and len(time_str) > 10:
        try:
            dt = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
            return dt.astimezone(timezone.utc)
        except ValueError:
            pass

    return None


def _empty_result():
    return {
        "macro_block": False,
        "macro_risk_level": "none",
        "macro_event_title": "",
        "macro_event_currency": "",
        "macro_event_time": "",
        "macro_minutes_distance": None,
        "macro_reason": "",
        "macro_context_snapshot": {
            "high_impact_events": [],
            "medium_impact_events": [],
            "next_event": None,
            "last_event": None,
            "affected_currencies": [],
            "has_macro_block": False,
            "has_confidence_reduction": False,
        },
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_macro_risk(pair, decision_time=None, events=None):
    """Return macro risk assessment for a trade decision.

    Parameters
    ----------
    pair : str
        Currency pair, e.g. "EUR/USD".
    decision_time : datetime, optional
        UTC datetime of the decision. Defaults to now.
    events : list, optional
        Calendar events in ff_calendar format (dicts with 'date', 'time',
        'currency', 'impact', 'event' keys). If None or lacking 'date' field,
        ff_calendar.fetch_this_week() is called (uses its own cache).

    Returns
    -------
    dict with keys:
        macro_block            bool
        macro_risk_level       "none" | "medium" | "high"
        macro_event_title      str
        macro_event_currency   str
        macro_event_time       str  (ISO UTC)
        macro_minutes_distance float | None  (negative = event already past)
        macro_reason           str
        macro_context_snapshot dict
    """
    try:
        return _inner(pair, decision_time, events)
    except Exception as exc:
        logger.warning("[macro_filter] non-fatal error: %s: %s", type(exc).__name__, exc)
        return _empty_result()


def _inner(pair, decision_time, events):
    if not _env_bool("USE_ECONOMIC_CALENDAR_FILTER", True):
        return _empty_result()

    if decision_time is None:
        decision_time = datetime.now(timezone.utc)
    if decision_time.tzinfo is None:
        decision_time = decision_time.replace(tzinfo=timezone.utc)

    high_block_before = _env_int("MACRO_HIGH_IMPACT_BLOCK_BEFORE_MINUTES", 30)
    high_block_after  = _env_int("MACRO_HIGH_IMPACT_BLOCK_AFTER_MINUTES", 30)
    medium_window     = _env_int("MACRO_MEDIUM_IMPACT_WINDOW_MINUTES", 20)

    # Prefer ff_calendar as authoritative source (has proper date + time fields).
    # Fall through to it if the caller didn't pass dated events.
    if not events or not any(e.get("date") for e in events):
        try:
            from modules import ff_calendar
            events, _, _, _ = ff_calendar.fetch_this_week()
        except Exception as exc:
            logger.warning("[macro_filter] calendar fetch failed (non-fatal): %s", exc)
            return _empty_result()

    if not events:
        return _empty_result()

    currencies = _pair_currencies(pair)
    high_nearby   = []   # events within the high-impact block window
    medium_nearby = []   # events within the medium-impact window
    all_relevant  = []   # all medium+high events for the pair currencies

    for ev in events:
        currency = (ev.get("currency") or "").strip().upper()
        impact   = (ev.get("impact")   or "").strip().lower()
        title    = ev.get("event") or ev.get("title") or ""

        if currencies and currency not in currencies:
            continue
        if impact not in ("high", "medium"):
            continue

        event_dt = _parse_event_dt(ev)
        if event_dt is None:
            continue

        # Signed minutes: positive = event is in the future, negative = past
        signed_min = (event_dt - decision_time).total_seconds() / 60

        info = {
            "title":            title,
            "currency":         currency,
            "impact":           impact,
            "event_time":       event_dt.isoformat(),
            "minutes_distance": round(signed_min, 1),
        }
        all_relevant.append(info)

        if impact == "high":
            # Block from high_block_after minutes in the past to
            # high_block_before minutes in the future.
            if -high_block_after <= signed_min <= high_block_before:
                high_nearby.append(info)
        elif impact == "medium":
            if abs(signed_min) <= medium_window:
                medium_nearby.append(info)

    all_relevant.sort(key=lambda e: e["minutes_distance"])
    future_evs = [e for e in all_relevant if e["minutes_distance"] > 0]
    past_evs   = [e for e in all_relevant if e["minutes_distance"] <= 0]

    snapshot = {
        "high_impact_events":     [e for e in all_relevant if e["impact"] == "high"],
        "medium_impact_events":   [e for e in all_relevant if e["impact"] == "medium"],
        "next_event":             future_evs[0]  if future_evs else None,
        "last_event":             past_evs[-1]   if past_evs   else None,
        "affected_currencies":    sorted(currencies),
        "has_macro_block":        bool(high_nearby),
        "has_confidence_reduction": bool(medium_nearby) and not bool(high_nearby),
    }

    result = _empty_result()
    result["macro_context_snapshot"] = snapshot

    # ── HIGH IMPACT: block ────────────────────────────────────────────────────
    if high_nearby:
        ev = min(high_nearby, key=lambda e: abs(e["minutes_distance"]))
        result.update({
            "macro_block":            True,
            "macro_risk_level":       "high",
            "macro_event_title":      ev["title"],
            "macro_event_currency":   ev["currency"],
            "macro_event_time":       ev["event_time"],
            "macro_minutes_distance": ev["minutes_distance"],
            "macro_reason":           "high_impact_macro_event",
        })
        return result

    # ── MEDIUM IMPACT: confidence reduction signal ────────────────────────────
    if medium_nearby:
        ev = min(medium_nearby, key=lambda e: abs(e["minutes_distance"]))
        result.update({
            "macro_block":            False,
            "macro_risk_level":       "medium",
            "macro_event_title":      ev["title"],
            "macro_event_currency":   ev["currency"],
            "macro_event_time":       ev["event_time"],
            "macro_minutes_distance": ev["minutes_distance"],
            "macro_reason":           "medium_impact_macro_event",
        })
        return result

    return result
