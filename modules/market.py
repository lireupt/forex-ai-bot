import os
from datetime import datetime, timezone


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
        return int(value)
    except ValueError:
        return default


def market_guard_config():
    return {
        "enabled": _env_bool("FOREX_MARKET_GUARD_ENABLED", True),
        "close_weekday": _env_int("FOREX_MARKET_CLOSE_WEEKDAY", 4),
        "close_hour_utc": _env_int("FOREX_MARKET_CLOSE_HOUR_UTC", 22),
        "open_weekday": _env_int("FOREX_MARKET_OPEN_WEEKDAY", 6),
        "open_hour_utc": _env_int("FOREX_MARKET_OPEN_HOUR_UTC", 22),
    }


def _as_utc(now_utc):
    if now_utc is None:
        return datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        return now_utc.replace(tzinfo=timezone.utc)
    return now_utc.astimezone(timezone.utc)


def forex_market_state(now_utc=None, config=None):
    config = config or market_guard_config()
    now = _as_utc(now_utc)
    if not config["enabled"]:
        return {
            "is_open": True,
            "reason": "",
            "gate": "market_guard_disabled",
            "checked_at": now.isoformat(),
            "config": config,
        }

    weekday = now.weekday()
    hour = now.hour
    close_weekday = config["close_weekday"]
    open_weekday = config["open_weekday"]
    close_hour = config["close_hour_utc"]
    open_hour = config["open_hour_utc"]

    closed = False
    gate = ""
    reason = ""
    if weekday == 5:
        closed = True
        gate = "market_closed_weekend"
        reason = "mercado fechado durante sábado UTC"
    elif weekday == open_weekday and hour < open_hour:
        closed = True
        gate = "market_closed_weekend"
        reason = f"mercado ainda fechado no domingo antes das {open_hour:02d}:00 UTC"
    elif weekday == close_weekday and hour >= close_hour:
        closed = True
        gate = "market_closed_session"
        reason = f"mercado fechado na sexta-feira após as {close_hour:02d}:00 UTC"

    return {
        "is_open": not closed,
        "reason": reason,
        "gate": gate or "market_open",
        "checked_at": now.isoformat(),
        "config": config,
    }


def is_forex_market_open(now_utc=None):
    return forex_market_state(now_utc=now_utc)["is_open"]
