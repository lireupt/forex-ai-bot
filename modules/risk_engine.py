def clamp(value, low, high):
    return max(low, min(high, value))


def _direction(signal):
    signal = (signal or "NEUTRAL").upper()
    if signal == "BUY":
        return 1
    if signal == "SELL":
        return -1
    return 0


def _as_float(value, default=0.0):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value, default=0):
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _append_adjustment(items, name, value, reason):
    if value:
        items.append({
            "name": name,
            "value": round(value, 4),
            "reason": reason,
        })


class AdaptiveRiskEngine:
    """Contextual gate for execution risk.

    Signal direction and score are inputs only. This engine may change execution
    permission and sizing, but never mutates BUY/SELL/NEUTRAL labels.
    """

    def __init__(self, config=None):
        self.config = config or {}

    def evaluate(
        self,
        signal,
        confidence,
        combined_signal=None,
        event_risk=None,
        atr_pips=None,
        technical_indicators=None,
        gate_context=None,
    ):
        combined_signal = combined_signal or {}
        event_risk = event_risk or {}
        technical_indicators = technical_indicators or {}
        gate_context = gate_context or {}

        signal = (signal or "NEUTRAL").upper()
        raw_confidence = clamp(_as_float(confidence, 0.0) / 100.0, 0.0, 1.0)
        combined_score = _as_float(
            gate_context.get("combined_score", combined_signal.get("combined_score")),
            0.0,
        )
        score_strength = abs(combined_score)

        effective_confidence = self._confidence_engine(
            raw_confidence=raw_confidence,
            score_strength=score_strength,
            signal=signal,
            combined_signal=combined_signal,
            event_risk=event_risk,
            atr_pips=atr_pips,
            technical_indicators=technical_indicators,
            gate_context=gate_context,
        )
        threshold = self._adaptive_threshold(
            signal=signal,
            combined_signal=combined_signal,
            event_risk=event_risk,
            atr_pips=atr_pips,
            technical_indicators=technical_indicators,
            gate_context=gate_context,
        )
        context_blocks = self._hard_blocks(
            signal=signal,
            gate_context=gate_context,
            event_risk=event_risk,
        )

        risk_multiplier, exposure_bucket = self._risk_multiplier(
            effective_confidence["value"],
            threshold["value"],
        )
        ai_risk_adjustment = clamp(_as_float(gate_context.get("ai_risk_adjustment"), 0.0), -0.5, 0.5)
        if ai_risk_adjustment < 0:
            risk_multiplier *= 1.0 + ai_risk_adjustment
        elif effective_confidence["value"] >= threshold["value"] + 0.10:
            risk_multiplier *= 1.0 + min(ai_risk_adjustment, 0.20)
        risk_multiplier = round(clamp(risk_multiplier, 0.0, 1.0), 4)

        allow_trade = not context_blocks and effective_confidence["value"] >= threshold["value"]
        if risk_multiplier <= 0:
            allow_trade = False

        if context_blocks:
            reason = context_blocks[0]
        elif allow_trade:
            reason = (
                f"adaptive_risk_allowed: confidence={effective_confidence['value']:.2f} "
                f">= threshold={threshold['value']:.2f}, risk_multiplier={risk_multiplier:.2f}"
            )
        else:
            reason = "confidence_below_threshold"

        return {
            "allow_trade": allow_trade,
            "trade_allowed": allow_trade,
            "risk_multiplier": risk_multiplier,
            "dynamic_exposure": exposure_bucket,
            "adaptive_min_confidence": round(threshold["value"], 4),
            "effective_confidence": round(effective_confidence["value"], 4),
            "raw_confidence": round(raw_confidence, 4),
            "score_strength": round(score_strength, 4),
            "execution_reason": reason,
            "block_reason": None if allow_trade else reason,
            "bonuses": effective_confidence["bonuses"] + threshold["bonuses"],
            "penalties": effective_confidence["penalties"] + threshold["penalties"],
            "context_blocks": context_blocks,
            "market_context": self._market_context(
                event_risk=event_risk,
                atr_pips=atr_pips,
                technical_indicators=technical_indicators,
                gate_context=gate_context,
            ),
        }

    def _confidence_engine(
        self,
        raw_confidence,
        score_strength,
        signal,
        combined_signal,
        event_risk,
        atr_pips,
        technical_indicators,
        gate_context,
    ):
        # Non-linear score contribution: 0.40-0.50 scores become moderate,
        # while very weak scores still need context to pass.
        score_confidence = clamp(0.18 + (score_strength * 0.95), 0.0, 0.85)
        value = max(raw_confidence, score_confidence)
        bonuses = []
        penalties = []

        ai_signal = (gate_context.get("ai_signal") or gate_context.get("ai_bias") or "").upper()
        ai_confidence = _as_float(gate_context.get("ai_confidence_score"), 0.0)
        if not ai_confidence:
            ai_confidence = clamp(_as_float(gate_context.get("ai_confidence"), 0.0) / 100.0, 0.0, 1.0)
        if ai_signal == signal and ai_confidence >= 0.70:
            value += 0.04
            _append_adjustment(bonuses, "ai_alignment", 0.04, "AI concordante com confiança alta")

        if self._mtf_aligned(signal, gate_context):
            value += 0.05
            _append_adjustment(bonuses, "mtf_alignment", 0.05, "timeframes superiores alinhados")

        persistence = _as_int(gate_context.get("signal_persistence"), 0)
        if persistence >= 3:
            bonus = min(0.08, 0.03 + ((persistence - 3) * 0.01))
            value += bonus
            _append_adjustment(bonuses, "signal_persistence", bonus, f"{persistence} decisões consecutivas na direção")
        elif persistence == 2:
            value += 0.02
            _append_adjustment(bonuses, "signal_persistence", 0.02, "2 decisões consecutivas na direção")

        atr = _as_float(atr_pips, 0.0)
        max_atr = _as_float(self.config.get("atr_extreme_pips"), 35.0)
        if atr and atr > max_atr:
            penalty = min(0.12, (atr - max_atr) / 100.0)
            value -= penalty
            _append_adjustment(penalties, "extreme_volatility", penalty, f"ATR {atr:.1f} pips acima do limite contextual")

        if event_risk.get("dangerous_event_nearby"):
            value -= 0.05
            _append_adjustment(penalties, "news_risk", 0.05, event_risk.get("dangerous_event_reason") or "evento macro de alto impacto próximo")

        return {
            "value": round(clamp(value, 0.0, 1.0), 4),
            "bonuses": bonuses,
            "penalties": penalties,
        }

    def _adaptive_threshold(
        self,
        signal,
        combined_signal,
        event_risk,
        atr_pips,
        technical_indicators,
        gate_context,
    ):
        value = _as_float(
            self.config.get(
                "min_confidence_to_trade",
                self.config.get("adaptive_base_min_confidence"),
            ),
            0.55,
        )
        bonuses = []
        penalties = []

        ai_signal = (gate_context.get("ai_signal") or gate_context.get("ai_bias") or "").upper()
        ai_confidence = _as_float(gate_context.get("ai_confidence_score"), 0.0)
        if not ai_confidence:
            ai_confidence = clamp(_as_float(gate_context.get("ai_confidence"), 0.0) / 100.0, 0.0, 1.0)
        if ai_signal == signal and ai_confidence >= 0.70:
            value -= 0.04
            _append_adjustment(bonuses, "lower_threshold_ai_alignment", 0.04, "AI concordante reduz threshold")

        if self._mtf_aligned(signal, gate_context):
            value -= 0.04
            _append_adjustment(bonuses, "lower_threshold_mtf_alignment", 0.04, "MTF alinhado reduz threshold")

        persistence = _as_int(gate_context.get("signal_persistence"), 0)
        if persistence >= 3:
            value -= 0.04
            _append_adjustment(bonuses, "lower_threshold_persistence", 0.04, "persistência temporal reduz threshold")

        spread_pips = _as_float(gate_context.get("spread_pips"), 0.0)
        max_spread = _as_float(self.config.get("max_spread_pips"), 2.5)
        if spread_pips and spread_pips > max_spread * 0.70:
            penalty = 0.03 if spread_pips <= max_spread else 0.08
            value += penalty
            _append_adjustment(penalties, "spread_penalty", penalty, f"spread={spread_pips:.1f} pips")

        atr = _as_float(atr_pips, 0.0)
        min_atr = _as_float(self.config.get("min_atr_pips"), 0.0)
        max_atr = _as_float(self.config.get("atr_extreme_pips"), 35.0)
        if min_atr and atr and atr < min_atr:
            value += 0.05
            _append_adjustment(penalties, "low_volatility_threshold", 0.05, f"ATR {atr:.1f} abaixo do mínimo operacional")
        if atr and atr > max_atr:
            value += 0.06
            _append_adjustment(penalties, "extreme_volatility_threshold", 0.06, f"ATR {atr:.1f} elevado")

        if event_risk.get("dangerous_event_nearby"):
            value += 0.08
            _append_adjustment(penalties, "news_threshold", 0.08, event_risk.get("dangerous_event_reason") or "evento macro de alto impacto próximo")

        market = gate_context.get("market") or {}
        if market.get("liquidity") == "low" or market.get("session") in {"rollover", "closed"}:
            value += 0.05
            _append_adjustment(penalties, "low_liquidity_session", 0.05, "sessão de baixa liquidez")

        performance = gate_context.get("performance") or {}
        loss_streak = _as_int(performance.get("loss_streak"), 0)
        if loss_streak >= 2:
            penalty = min(0.10, loss_streak * 0.03)
            value += penalty
            _append_adjustment(penalties, "loss_streak", penalty, f"{loss_streak} perdas consecutivas")

        drawdown = _as_float(performance.get("max_drawdown"), 0.0)
        if drawdown >= _as_float(self.config.get("drawdown_penalty_start_r"), 3.0):
            value += 0.07
            _append_adjustment(penalties, "drawdown", 0.07, f"drawdown recente {drawdown:.2f}R")

        low = _as_float(self.config.get("adaptive_min_floor"), 0.35)
        high = _as_float(self.config.get("adaptive_min_ceiling"), 0.65)
        return {
            "value": round(clamp(value, low, high), 4),
            "bonuses": bonuses,
            "penalties": penalties,
        }

    def _hard_blocks(self, signal, gate_context, event_risk):
        blocks = []
        if signal not in ("BUY", "SELL"):
            blocks.append("signal_not_tradeable")

        spread_pips = _as_float(gate_context.get("spread_pips"), 0.0)
        max_spread = _as_float(self.config.get("max_spread_pips"), 2.5)
        if spread_pips and spread_pips > max_spread:
            blocks.append("spread_above_max")

        market = gate_context.get("market") or {}
        if market and not market.get("is_open", True):
            blocks.append(market.get("gate") or "market_closed")

        operational = gate_context.get("operational") or {}
        if operational and not operational.get("can_open_trade", True):
            blocks.append(operational.get("block_reason") or "outside_operational_trade_window")

        cooldown = gate_context.get("cooldown") or {}
        if cooldown.get("cooldown_active"):
            blocks.append("cooldown_active")
        if cooldown.get("max_direction_signals_reached"):
            blocks.append("max_direction_signals_reached")

        event_block_enabled = (
            self.config.get("block_near_high_impact_events")
            or self.config.get("block_extreme_news_risk")
        )
        if event_block_enabled and event_risk.get("dangerous_event_nearby"):
            blocks.append("high_impact_event_nearby")

        out = []
        for block in blocks:
            if block and block not in out:
                out.append(block)
        return out

    def _risk_multiplier(self, effective_confidence, adaptive_min_confidence):
        if effective_confidence < 0.35:
            return 0.0, "blocked"
        if effective_confidence < adaptive_min_confidence:
            return 0.0, "below_adaptive_threshold"
        if effective_confidence < 0.45:
            return 0.25, "micro"
        if effective_confidence < 0.55:
            return 0.50, "small"
        if effective_confidence < 0.65:
            return 0.75, "normal"
        return 1.0, "full"

    def _mtf_aligned(self, signal, gate_context):
        alignment = gate_context.get("timeframe_alignment") or ""
        if "aligned" in alignment and "against" not in alignment:
            return True
        mtf_signal = (gate_context.get("mtf_signal") or "").upper()
        return mtf_signal == signal

    def _market_context(self, event_risk, atr_pips, technical_indicators, gate_context):
        performance = gate_context.get("performance") or {}
        return {
            "atr_pips": round(_as_float(atr_pips, 0.0), 2) if atr_pips is not None else None,
            "spread_pips": gate_context.get("spread_pips"),
            "market": gate_context.get("market") or {},
            "event": {
                "dangerous_event_nearby": bool(event_risk.get("dangerous_event_nearby")),
                "reason": event_risk.get("dangerous_event_reason", ""),
            },
            "signal_persistence": _as_int(gate_context.get("signal_persistence"), 0),
            "timeframe_alignment": gate_context.get("timeframe_alignment"),
            "performance": {
                "loss_streak": _as_int(performance.get("loss_streak"), 0),
                "max_drawdown": performance.get("max_drawdown"),
            },
        }
