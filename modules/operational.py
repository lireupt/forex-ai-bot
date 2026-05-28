"""Janelas operacionais para separar análise de abertura de trades."""

import os
from datetime import datetime


def _env_int(name, default):
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return int(value)


def operational_state(now_dt=None, mode="trade", tolerance_minutes=0):
    now_dt = now_dt or datetime.now().astimezone()
    mode = (mode or "trade").strip().lower()
    if mode not in {"analysis", "trade"}:
        mode = "trade"
    start_hour = _env_int("OPERATIONAL_TRADE_START_HOUR", 7)
    end_hour = _env_int("OPERATIONAL_TRADE_END_HOUR", 15)

    state = {
        "mode": mode,
        "can_open_trade": False,
        "block_reason": "",
        "is_weekend": now_dt.weekday() >= 5,
        "is_night_collection": 0 <= now_dt.hour < 6,
        "trade_start_hour": start_hour,
        "trade_end_hour": end_hour,
        "allowed_trade_window": f"{start_hour:02d}:00-{end_hour:02d}:00",
        "allowed_trade_windows": [f"{start_hour:02d}:00-{end_hour:02d}:00"],
        "current_time": now_dt.isoformat(),
    }

    if mode == "analysis":
        state["block_reason"] = "analysis_mode"
        return state

    if state["is_weekend"]:
        state["block_reason"] = "weekend"
        return state

    if state["is_night_collection"]:
        state["block_reason"] = "night_collection_only"
        return state

    start_minute = max(0, min(23, start_hour)) * 60
    end_minute = max(1, min(24, end_hour)) * 60
    minute_of_day = now_dt.hour * 60 + now_dt.minute
    tolerance = max(0, int(tolerance_minutes or 0))
    if start_minute - tolerance <= minute_of_day < end_minute + tolerance:
        state["can_open_trade"] = True
        return state

    state["block_reason"] = "outside_operational_trade_window"
    return state
