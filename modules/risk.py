import os

PIP_SIZE = 0.0001
DEFAULT_MIN_CONFIDENCE = 65
DEFAULT_ACCOUNT_BALANCE = 1000.0
DEFAULT_RISK_PERCENT = 1.0
DEFAULT_STOP_LOSS_PIPS = 30.0
DEFAULT_TAKE_PROFIT_PIPS = 60.0
DEFAULT_EVENT_BLOCK_WINDOW_MINUTES = 120
DEFAULT_USE_ATR_SL_TP = True
DEFAULT_ATR_SL_MULT = 1.5
DEFAULT_ATR_TP_MULT = 3.0
DEFAULT_ATR_MIN_SL_PIPS = 12.0
DEFAULT_ATR_MAX_SL_PIPS = 60.0


def _env_bool(name, default):
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_float(name, default):
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return float(value)


def load_risk_config():
    return {
        "dry_run": _env_bool("DRY_RUN", True),
        "min_confidence": int(_env_float("MIN_CONFIDENCE", DEFAULT_MIN_CONFIDENCE)),
        "account_balance": _env_float("ACCOUNT_BALANCE", DEFAULT_ACCOUNT_BALANCE),
        "risk_per_trade_percent": _env_float("RISK_PER_TRADE_PERCENT", DEFAULT_RISK_PERCENT),
        "stop_loss_pips": _env_float("DEFAULT_STOP_LOSS_PIPS", DEFAULT_STOP_LOSS_PIPS),
        "take_profit_pips": _env_float("DEFAULT_TAKE_PROFIT_PIPS", DEFAULT_TAKE_PROFIT_PIPS),
        "block_near_high_impact_events": _env_bool("BLOCK_NEAR_HIGH_IMPACT_EVENTS", True),
        "event_block_window_minutes": int(_env_float(
            "EVENT_BLOCK_WINDOW_MINUTES",
            DEFAULT_EVENT_BLOCK_WINDOW_MINUTES,
        )),
        "use_atr_sl_tp": _env_bool("USE_ATR_SL_TP", DEFAULT_USE_ATR_SL_TP),
        "atr_sl_mult": _env_float("ATR_SL_MULT", DEFAULT_ATR_SL_MULT),
        "atr_tp_mult": _env_float("ATR_TP_MULT", DEFAULT_ATR_TP_MULT),
        "atr_min_sl_pips": _env_float("ATR_MIN_SL_PIPS", DEFAULT_ATR_MIN_SL_PIPS),
        "atr_max_sl_pips": _env_float("ATR_MAX_SL_PIPS", DEFAULT_ATR_MAX_SL_PIPS),
    }


def _validate_config(config):
    checks = {
        "ACCOUNT_BALANCE": config["account_balance"],
        "RISK_PER_TRADE_PERCENT": config["risk_per_trade_percent"],
        "DEFAULT_STOP_LOSS_PIPS": config["stop_loss_pips"],
        "DEFAULT_TAKE_PROFIT_PIPS": config["take_profit_pips"],
    }
    invalid = [name for name, value in checks.items() if value <= 0]
    if invalid:
        return f"config inválida: {', '.join(invalid)} tem de ser > 0"
    if config["min_confidence"] < 0 or config["min_confidence"] > 100:
        return "config inválida: MIN_CONFIDENCE tem de estar entre 0 e 100"
    if config["event_block_window_minutes"] < 0:
        return "config inválida: EVENT_BLOCK_WINDOW_MINUTES tem de ser >= 0"
    return None


def _resolve_sl_tp_pips(config, atr_pips):
    if config["use_atr_sl_tp"] and atr_pips is not None and atr_pips > 0:
        sl = atr_pips * config["atr_sl_mult"]
        sl = max(config["atr_min_sl_pips"], min(config["atr_max_sl_pips"], sl))
        tp = sl * (config["atr_tp_mult"] / config["atr_sl_mult"])
        return round(sl, 1), round(tp, 1), "atr"
    return (
        round(config["stop_loss_pips"], 1),
        round(config["take_profit_pips"], 1),
        "fixed",
    )


def _build_order(pair, signal, confidence, current_price, config, reason, atr_pips=None):
    sl_pips, tp_pips, sl_tp_mode = _resolve_sl_tp_pips(config, atr_pips)
    risk_amount = config["account_balance"] * (config["risk_per_trade_percent"] / 100)
    position_size = risk_amount / (sl_pips * PIP_SIZE)

    if signal == "BUY":
        stop_loss = current_price - (sl_pips * PIP_SIZE)
        take_profit = current_price + (tp_pips * PIP_SIZE)
    else:
        stop_loss = current_price + (sl_pips * PIP_SIZE)
        take_profit = current_price - (tp_pips * PIP_SIZE)

    return {
        "mode": "DRY_RUN",
        "pair": pair,
        "signal": signal,
        "entry_price": round(current_price, 5),
        "stop_loss": round(stop_loss, 5),
        "take_profit": round(take_profit, 5),
        "stop_loss_pips": sl_pips,
        "take_profit_pips": tp_pips,
        "sl_tp_mode": sl_tp_mode,
        "atr_pips_used": round(atr_pips, 1) if atr_pips is not None else None,
        "confidence": confidence,
        "risk_percent": config["risk_per_trade_percent"],
        "risk_amount": round(risk_amount, 2),
        "estimated_position_size": round(position_size),
        "reason": reason,
    }


def evaluate_trade(pair, combined_signal, current_price, event_risk=None, atr_pips=None):
    config = load_risk_config()
    signal = combined_signal.get("signal", "NEUTRAL")
    confidence = int(combined_signal.get("confidence", 0))
    hold_off = bool(combined_signal.get("hold_off", True))
    reason = combined_signal.get("reasoning", "")
    event_risk = event_risk or {
        "dangerous_event_nearby": False,
        "dangerous_event_reason": "",
    }

    result = {
        "trade_allowed": False,
        "block_reason": None,
        "simulated_order": None,
        "config": config,
        "dangerous_event_nearby": bool(event_risk.get("dangerous_event_nearby")),
        "dangerous_event_reason": event_risk.get("dangerous_event_reason", ""),
    }

    if not config["dry_run"]:
        result["block_reason"] = "DRY_RUN está desativado; execução real não está implementada"
        return result

    config_error = _validate_config(config)
    if config_error:
        result["block_reason"] = config_error
        return result

    if signal == "NEUTRAL":
        result["block_reason"] = "sinal combinado é NEUTRAL"
        return result

    if hold_off:
        result["block_reason"] = "hold_off está ativo"
        return result

    if confidence < config["min_confidence"]:
        result["block_reason"] = f"confiança {confidence}% abaixo do mínimo {config['min_confidence']}%"
        return result

    if current_price is None or current_price <= 0:
        result["block_reason"] = "preço actual indisponível"
        return result

    if config["block_near_high_impact_events"] and result["dangerous_event_nearby"]:
        result["block_reason"] = result["dangerous_event_reason"]
        return result

    result["trade_allowed"] = True
    result["simulated_order"] = _build_order(
        pair=pair,
        signal=signal,
        confidence=confidence,
        current_price=float(current_price),
        config=config,
        reason=reason,
        atr_pips=atr_pips,
    )
    return result
