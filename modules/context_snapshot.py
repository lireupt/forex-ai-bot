"""Empacotamento do snapshot de mercado para a IA agregadora (Camada 4).

Não faz I/O pesado nem decisões: apenas estrutura, a partir de objetos já
calculados no pipeline, o snapshot que a IA agregadora recebe. A única consulta
extra é o resumo de calibração recente (Camada 3 — performance/contexto).
"""

from datetime import datetime, timedelta, timezone

from modules import database


def _since(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def build_performance_snapshot(conn, pair, recent_performance=None, days=7):
    """Camada 3 — performance/contexto recente.

    Combina o resumo de calibração (winrate, net_pips, blocked_by_reason,
    buy_vs_sell, expectancy) com a leitura de risco recente já calculada no
    pipeline (loss_streak, max_drawdown), quando disponível.
    """
    recent_performance = recent_performance or {}
    try:
        summary = database.get_calibration_summary(conn, since_iso=_since(days), pair=pair)
    except Exception:
        summary = {}

    return {
        "window_days": days,
        "total_decisions": summary.get("total_decisions"),
        "total_executed": summary.get("total_executed"),
        "total_blocked": summary.get("total_blocked"),
        "winrate": summary.get("winrate", recent_performance.get("winrate")),
        "net_pips": summary.get("net_pips"),
        "expectancy": summary.get("expectancy", recent_performance.get("expectancy")),
        "profit_factor": summary.get("profit_factor"),
        "buy_vs_sell": summary.get("buy_vs_sell"),
        "best_direction": summary.get("best_direction"),
        "blocked_by_reason": summary.get("blocked_by_reason"),
        "loss_streak": recent_performance.get("loss_streak"),
        "max_drawdown": recent_performance.get("max_drawdown"),
    }


def build_market_snapshot(
    pair,
    technical_result,
    ai_result,
    combined,
    gating_combined,
    trade_decision,
    gate_context,
    event_risk,
    performance,
    gating_mode=None,
):
    """Monta o snapshot estruturado completo (input da IA agregadora)."""
    technical_result = technical_result or {}
    indicators = technical_result.get("indicators") or {}
    ai_result = ai_result or {}
    combined = combined or {}
    gating_combined = gating_combined or {}
    trade_decision = trade_decision or {}
    gate_context = gate_context or {}
    event_risk = event_risk or {}
    market = gate_context.get("market") or {}
    operational = gate_context.get("operational") or {}
    cooldown = gate_context.get("cooldown") or {}
    config = (trade_decision.get("gate_diagnostics") or {}).get("config") or {}

    technical = {
        "current_price": indicators.get("current_price"),
        "rsi": indicators.get("rsi"),
        "rsi_signal": indicators.get("rsi_signal"),
        "ema20": indicators.get("ema20"),
        "ema50": indicators.get("ema50"),
        "ema_trend": indicators.get("ema_trend"),
        "macd": indicators.get("macd"),
        "macd_signal": indicators.get("macd_signal"),
        "macd_signal_value": indicators.get("macd_signal_value"),
        "atr_pips": indicators.get("atr_pips"),
        "volatility_reason": indicators.get("volatility_reason"),
        "adx": indicators.get("adx"),
        "technical_signal": technical_result.get("signal"),
        "technical_score": indicators.get("technical_score"),
        "technical_score_m15": technical_result.get("technical_score_m15"),
        "technical_score_h1": technical_result.get("technical_score_h1"),
        "technical_score_h4": technical_result.get("technical_score_h4"),
        "technical_score_d1": technical_result.get("technical_score_d1"),
        "multi_timeframe_score": technical_result.get("multi_timeframe_score"),
        "timeframe_alignment": technical_result.get("timeframe_alignment"),
    }

    fundamental = {
        "ai_bias": ai_result.get("bias", ai_result.get("signal")),
        "ai_confidence": ai_result.get("confidence"),
        "macro_context": ai_result.get("macro_context"),
        "news_sentiment": ai_result.get("news_sentiment"),
        "volatility_context": ai_result.get("volatility_context"),
        "ai_reason": ai_result.get("reason", ai_result.get("reasoning")),
        "dangerous_event_nearby": bool(event_risk.get("dangerous_event_nearby")),
        "dangerous_event_reason": event_risk.get("dangerous_event_reason"),
    }

    operational_risk = {
        "market_open": market.get("is_open"),
        "session": market.get("session"),
        "can_open_trade": operational.get("can_open_trade"),
        "operational_block_reason": operational.get("block_reason"),
        "cooldown_active": bool(cooldown.get("cooldown_active")),
        "signal_persistence": gate_context.get("signal_persistence"),
        "spread_pips": gate_context.get("spread_pips"),
    }

    filters = {
        "dry_run": config.get("dry_run", True),
        "allow_buy": config.get("allow_buy"),
        "allow_sell": config.get("allow_sell"),
        "block_near_high_impact_events": config.get("block_near_high_impact_events"),
        "gate_reasons": trade_decision.get("gate_reasons"),
        "block_reason": trade_decision.get("block_reason"),
        "trade_allowed": trade_decision.get("trade_allowed"),
    }

    preliminary_recommendation = {
        "combined_signal": combined.get("signal"),
        "combined_confidence": combined.get("confidence"),
        "combined_score": combined.get("combined_score"),
        "gating_signal": gating_combined.get("signal"),
        "gating_mode": gating_mode,
        "hold_off": bool(gating_combined.get("hold_off")),
    }

    return {
        "pair": pair,
        "technical": technical,
        "fundamental": fundamental,
        "performance": performance,
        "operational_risk": operational_risk,
        "filters": filters,
        "preliminary_recommendation": preliminary_recommendation,
    }
