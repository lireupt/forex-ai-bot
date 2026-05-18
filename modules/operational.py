"""Janelas operacionais para separar análise de abertura de trades."""

from datetime import datetime


TRADE_WINDOWS = ((6, 5), (8, 5), (10, 5), (13, 35), (15, 5), (17, 5))


def operational_state(now_dt=None, mode="trade", tolerance_minutes=0):
    now_dt = now_dt or datetime.now().astimezone()
    mode = (mode or "trade").strip().lower()
    if mode not in {"analysis", "trade"}:
        mode = "trade"

    state = {
        "mode": mode,
        "can_open_trade": False,
        "block_reason": "",
        "is_weekend": now_dt.weekday() >= 5,
        "is_night_collection": 0 <= now_dt.hour < 6,
        "allowed_trade_windows": [f"{h:02d}:{m:02d}" for h, m in TRADE_WINDOWS],
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

    minute_of_day = now_dt.hour * 60 + now_dt.minute
    tolerance = max(0, int(tolerance_minutes or 0))
    for hour, minute in TRADE_WINDOWS:
        allowed = hour * 60 + minute
        if abs(minute_of_day - allowed) <= tolerance:
            state["can_open_trade"] = True
            return state

    state["block_reason"] = "outside_operational_trade_window"
    return state
