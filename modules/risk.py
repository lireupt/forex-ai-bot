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
DEFAULT_MIN_ATR_PIPS = 8.5
DEFAULT_MIN_ADX = 20.0
DEFAULT_BUY_MIN_RSI = 55.0
DEFAULT_SELL_MAX_RSI = 45.0


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
        "allow_buy": _env_bool("ALLOW_BUY", True),
        "allow_sell": _env_bool("ALLOW_SELL", True),
        "atr_filter_enabled": _env_bool("ATR_FILTER_ENABLED", True),
        "min_atr_pips": _env_float("MIN_ATR_PIPS", DEFAULT_MIN_ATR_PIPS),
        "momentum_filter_enabled": _env_bool("MOMENTUM_FILTER_ENABLED", True),
        "min_adx": _env_float("MIN_ADX", DEFAULT_MIN_ADX),
        "buy_min_rsi": _env_float("BUY_MIN_RSI", DEFAULT_BUY_MIN_RSI),
        "sell_max_rsi": _env_float("SELL_MAX_RSI", DEFAULT_SELL_MAX_RSI),
        "require_ema_direction": _env_bool("REQUIRE_EMA_DIRECTION", True),
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


def _apply_risk_adjustment(config, adjustment):
    try:
        adj = float(adjustment or 0.0)
    except (TypeError, ValueError):
        adj = 0.0
    adj = max(-0.5, min(0.5, adj))
    adjusted = config["risk_per_trade_percent"] * (1.0 + adj)
    return max(0.1, round(adjusted, 4))


def _append_gate(result, reason):
    result.setdefault("gate_reasons", [])
    if reason and reason not in result["gate_reasons"]:
        result["gate_reasons"].append(reason)
    if reason and not result.get("block_reason"):
        result["block_reason"] = reason


def _technical_gate_block(signal, indicators, config):
    indicators = indicators or {}
    blocks = []
    rsi = indicators.get("rsi")
    ema20 = indicators.get("ema20")
    ema50 = indicators.get("ema50")
    adx = indicators.get("adx") if indicators.get("adx") is not None else indicators.get("adx14")

    try:
        rsi_v = float(rsi) if rsi is not None else None
        ema20_v = float(ema20) if ema20 is not None else None
        ema50_v = float(ema50) if ema50 is not None else None
        adx_v = float(adx) if adx is not None else None
    except (TypeError, ValueError):
        blocks.append("trend_filter_blocked")
        return blocks

    if adx_v is None or adx_v < config["min_adx"]:
        blocks.extend(["trend_filter_blocked", "adx_too_low"])

    if signal == "BUY":
        if rsi_v is None or rsi_v < config["buy_min_rsi"]:
            blocks.extend(["trend_filter_blocked", "rsi_momentum_blocked"])
        if config["require_ema_direction"] and (ema20_v is None or ema50_v is None or ema20_v <= ema50_v):
            blocks.extend(["trend_filter_blocked", "ema_direction_blocked"])
    elif signal == "SELL":
        if rsi_v is None or rsi_v > config["sell_max_rsi"]:
            blocks.extend(["trend_filter_blocked", "rsi_momentum_blocked"])
        if config["require_ema_direction"] and (ema20_v is None or ema50_v is None or ema20_v >= ema50_v):
            blocks.extend(["trend_filter_blocked", "ema_direction_blocked"])

    out = []
    for block in blocks:
        if block not in out:
            out.append(block)
    return out


def evaluate_trade(
    pair,
    combined_signal,
    current_price,
    event_risk=None,
    atr_pips=None,
    technical_indicators=None,
    gate_context=None,
):
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
        "gate_reasons": [],
        "gate_diagnostics": {
            "technical": technical_indicators or {},
            "context": gate_context or {},
            "config": {
                "allow_buy": config["allow_buy"],
                "allow_sell": config["allow_sell"],
                "atr_filter_enabled": config["atr_filter_enabled"],
                "min_atr_pips": config["min_atr_pips"],
                "momentum_filter_enabled": config["momentum_filter_enabled"],
                "min_adx": config["min_adx"],
                "buy_min_rsi": config["buy_min_rsi"],
                "sell_max_rsi": config["sell_max_rsi"],
                "require_ema_direction": config["require_ema_direction"],
            },
        },
    }

    if not config["dry_run"]:
        result["block_reason"] = "DRY_RUN está desativado; execução real não está implementada"
        return result

    config_error = _validate_config(config)
    if config_error:
        result["block_reason"] = config_error
        return result

    if signal == "NEUTRAL":
        result["block_reason"] = combined_signal.get("neutral_reason") or "sinal combinado é NEUTRAL"
        _append_gate(result, combined_signal.get("neutral_reason") or "weak_signal")
        return result

    if signal == "BUY" and not config["allow_buy"]:
        _append_gate(result, "buy_disabled")
        return result

    if signal == "SELL" and not config["allow_sell"]:
        _append_gate(result, "sell_disabled")
        return result

    if hold_off:
        _append_gate(result, "hold_off está ativo")
        return result

    if confidence < config["min_confidence"]:
        result["block_reason"] = f"confiança {confidence}% abaixo do mínimo {config['min_confidence']}%"
        return result

    if current_price is None or current_price <= 0:
        _append_gate(result, "preço actual indisponível")
        return result

    if config["block_near_high_impact_events"] and result["dangerous_event_nearby"]:
        _append_gate(result, "high_impact_event_nearby")
        return result

    market_state = (gate_context or {}).get("market") or {}
    if market_state and not market_state.get("is_open", True):
        _append_gate(result, "market_closed")
        _append_gate(result, market_state.get("gate") or "market_closed_session")
        return result

    operational = (gate_context or {}).get("operational") or {}
    if operational and not operational.get("can_open_trade", True):
        _append_gate(result, operational.get("block_reason") or "outside_operational_trade_window")
        return result

    cooldown = (gate_context or {}).get("cooldown") or {}
    if cooldown.get("cooldown_active"):
        _append_gate(result, "cooldown_active")
        return result
    if cooldown.get("max_direction_signals_reached"):
        _append_gate(result, "max_direction_signals_reached")
        return result

    if config["atr_filter_enabled"]:
        if atr_pips is None or float(atr_pips) < config["min_atr_pips"]:
            _append_gate(result, "low_volatility_block")
            return result

    if config["momentum_filter_enabled"]:
        blocks = _technical_gate_block(signal, technical_indicators, config)
        if blocks:
            for block in blocks:
                _append_gate(result, block)
            return result

    risk_adjustment = (gate_context or {}).get("ai_risk_adjustment", 0.0)
    order_config = dict(config)
    order_config["risk_per_trade_percent"] = _apply_risk_adjustment(config, risk_adjustment)

    result["trade_allowed"] = True
    result["simulated_order"] = _build_order(
        pair=pair,
        signal=signal,
        confidence=confidence,
        current_price=float(current_price),
        config=order_config,
        reason=reason,
        atr_pips=atr_pips,
    )
    return result
