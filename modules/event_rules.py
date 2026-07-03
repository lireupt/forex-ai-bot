"""Regras puras sobre eventos de calendário — sem I/O.

Extraído de `modules/database.py` para ser partilhado entre a query live
(`database.find_high_impact_event_nearby`) e o motor de decisão puro
(`modules/decision_engine.py`), sem criar um import circular entre os dois.
"""

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

HIGH_IMPACT_EVENT_WHITELIST = (
    "CPI",
    "Core CPI",
    "PCE",
    "Core PCE",
    "Nonfarm Payrolls",
    "NFP",
    "Unemployment Rate",
    "GDP",
    "Retail Sales",
    "PMI",
    "ISM",
    "FOMC",
    "Fed Rate Decision",
    "ECB Rate Decision",
    "ECB Press Conference",
    "Interest Rate Decision",
    "Powell Speech",
    "Lagarde Speech",
)


def parse_event_time(value):
    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError, OverflowError):
            return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def event_is_whitelisted(title):
    title_upper = (title or "").upper()
    return any(item.upper() in title_upper for item in HIGH_IMPACT_EVENT_WHITELIST)
