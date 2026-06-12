import os

import pandas as pd
import pandas_ta as ta

PIP_SIZE = 0.0001


def _neutral_result():
    return {
        "signal": "NEUTRAL",
        "confidence": 0,
        "shadow_technical_signal": "NEUTRAL",
        "shadow_technical_confidence": 0,
        "shadow_technical_reason": "Sem candles ou indicadores suficientes para análise técnica.",
        "technical_reason": "Sem candles ou indicadores suficientes para análise técnica.",
        "indicators": {
            "rsi": None,
            "rsi_signal": "neutral",
            "rsi_vote": "neutral",
            "ema20": None,
            "ema50": None,
            "ema200": None,
            "ema_trend": "neutral",
            "ema_vote": "neutral",
            "macd": None,
            "macd_signal": "neutral",
            "macd_signal_value": None,
            "macd_vote": "neutral",
            "macd_minus_signal": None,
            "adx": None,
            "adx14": None,
            "adx_vote": "neutral",
            "atr14": None,
            "atr_price": None,
            "atr_pips": None,
            "volatility_reason": "ATR indisponível.",
            "current_price": None,
            "ema20_minus_ema50": None,
            "structure": "neutral",
            "volume_spike": False,
            "technical_score": 0.0,
            "technical_signal": "NEUTRAL",
            "technical_score_reasons": [],
        },
    }


def _round_or_none(value, digits=4):
    if pd.isna(value):
        return None
    return round(float(value), digits)


def _env_float(name, default):
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _env_str(name, default):
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _env_bool(name, default):
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _vote_value(vote):
    return {"bullish": 1.0, "bearish": -1.0}.get(vote, 0.0)


def _technical_score(rsi_vote, ema_vote, macd_vote):
    weights = {
        "rsi": _env_float("RSI_WEIGHT", 0.30),
        "ema": _env_float("EMA_WEIGHT", 0.40),
        "macd": _env_float("MACD_WEIGHT", 0.30),
    }
    total_weight = sum(abs(v) for v in weights.values())
    if total_weight == 0:
        return 0.0, weights
    score = (
        _vote_value(rsi_vote) * weights["rsi"]
        + _vote_value(ema_vote) * weights["ema"]
        + _vote_value(macd_vote) * weights["macd"]
    ) / total_weight
    return max(-1.0, min(1.0, score)), weights


def _score_signal(score):
    buy_threshold = _env_float("TECHNICAL_BUY_THRESHOLD", 0.35)
    sell_threshold = _env_float("TECHNICAL_SELL_THRESHOLD", -0.35)
    if score >= buy_threshold:
        return "BUY"
    if score <= sell_threshold:
        return "SELL"
    return "NEUTRAL"


def _shadow_signal(votes):
    bullish_votes = votes.count("bullish")
    bearish_votes = votes.count("bearish")

    if bullish_votes >= 2 and bearish_votes == 0:
        signal = "BUY"
    elif bearish_votes >= 2 and bullish_votes == 0:
        signal = "SELL"
    else:
        signal = "NEUTRAL"

    confidence = round((max(bullish_votes, bearish_votes, votes.count("neutral")) / 3) * 100)
    reason = f"{bullish_votes} bullish votes, {bearish_votes} bearish votes"
    return signal, confidence, reason


def _atr_reason(atr_pips):
    if atr_pips is None:
        return "ATR indisponível."
    if atr_pips < 8:
        return f"Volatilidade baixa: ATR14 em {atr_pips} pips."
    if atr_pips <= 20:
        return f"Volatilidade normal: ATR14 em {atr_pips} pips."
    return f"Volatilidade elevada: ATR14 em {atr_pips} pips."


def _market_structure(df, lookback=6):
    if df is None or len(df) < lookback:
        return "neutral"
    recent = df.tail(lookback)
    first_high = float(recent["high"].iloc[0])
    last_high = float(recent["high"].iloc[-1])
    first_low = float(recent["low"].iloc[0])
    last_low = float(recent["low"].iloc[-1])
    if last_high > first_high and last_low > first_low:
        return "bullish"
    if last_high < first_high and last_low < first_low:
        return "bearish"
    return "neutral"


def _volume_spike(df, lookback=20, multiplier=1.5):
    if df is None or "volume" not in df.columns or len(df) < lookback + 1:
        return False
    recent = df["volume"].tail(lookback + 1)
    avg = recent.iloc[:-1].mean()
    if not avg or pd.isna(avg):
        return False
    return float(recent.iloc[-1]) >= float(avg) * multiplier


def analyse(candles_df, pair="EUR/USD", timeframe_role=None):
    try:
        if candles_df is None or candles_df.empty:
            return _neutral_result()

        df = candles_df.copy()
        if "close" not in df.columns:
            return _neutral_result()

        df["RSI_14"] = ta.rsi(df["close"], length=14)
        df["EMA_20"] = ta.ema(df["close"], length=20)
        df["EMA_50"] = ta.ema(df["close"], length=50)
        df["EMA_200"] = ta.ema(df["close"], length=200)
        df["ATR_14"] = ta.atr(df["high"], df["low"], df["close"], length=14)
        adx = ta.adx(df["high"], df["low"], df["close"], length=14)
        if adx is not None and not adx.empty:
            df = pd.concat([df, adx], axis=1)

        macd = ta.macd(df["close"], fast=12, slow=26, signal=9)
        if macd is not None and not macd.empty:
            df = pd.concat([df, macd], axis=1)

        required = [
            "RSI_14",
            "EMA_20",
            "EMA_50",
            "ATR_14",
            "MACD_12_26_9",
            "MACDs_12_26_9",
        ]
        role = (timeframe_role or "").strip().lower()
        if role in {"h4", "d1"}:
            required.append("EMA_200")
        if "ADX_14" in df.columns:
            required.append("ADX_14")
        usable = df.dropna(subset=required)
        if usable.empty:
            return _neutral_result()

        last = usable.iloc[-1]
        rsi = float(last["RSI_14"])
        ema20 = float(last["EMA_20"])
        ema50 = float(last["EMA_50"])
        ema200 = float(last["EMA_200"]) if "EMA_200" in usable.columns and not pd.isna(last["EMA_200"]) else None
        macd_value = float(last["MACD_12_26_9"])
        macd_signal_value = float(last["MACDs_12_26_9"])
        adx14 = float(last["ADX_14"]) if "ADX_14" in usable.columns else None
        atr14 = float(last["ATR_14"])
        atr_pips = atr14 / PIP_SIZE
        current_price = float(last["close"])

        if rsi < 35:
            rsi_signal = "bullish"
        elif rsi > 65:
            rsi_signal = "bearish"
        else:
            rsi_signal = "neutral"

        ema_delta = (ema50 - ema200) if role in {"h4", "d1"} and ema200 is not None else ema20 - ema50
        macd_delta = macd_value - macd_signal_value
        ema_trend = "bullish" if ema_delta > 0 else "bearish"
        macd_signal = "bullish" if macd_delta > 0 else "bearish"
        adx_vote = "trend" if adx14 is not None and adx14 >= _env_float("MIN_ADX", 20.0) else "weak"
        structure = _market_structure(usable)
        volume_spike = _volume_spike(usable)

        if role == "m15":
            votes = [rsi_signal, ema_trend, "bullish" if volume_spike and ema_trend == "bullish" else "bearish" if volume_spike and ema_trend == "bearish" else "neutral"]
        elif role == "h1":
            votes = [ema_trend, macd_signal, structure]
        elif role == "h4":
            votes = [ema_trend, macd_signal, structure if adx_vote == "trend" else "neutral"]
        elif role == "d1":
            votes = [ema_trend, structure, "neutral"]
        else:
            votes = [rsi_signal, ema_trend, macd_signal]
        bullish_votes = votes.count("bullish")
        bearish_votes = votes.count("bearish")
        neutral_votes = votes.count("neutral")

        score, weights = _technical_score(votes[0], votes[1], votes[2])
        mode = _env_str("TECHNICAL_SIGNAL_MODE", "score").lower()
        if mode == "strict":
            if bullish_votes == 3:
                signal = "BUY"
            elif bearish_votes == 3:
                signal = "SELL"
            else:
                signal = "NEUTRAL"
        else:
            signal = _score_signal(score)

        # ADX trend filter: H1 em lateral (ADX fraco) → NEUTRAL para evitar whipsaws.
        adx_filter_reason = ""
        if (
            role == "h1"
            and _env_bool("ADX_TREND_FILTER_ENABLED", False)
            and adx_vote == "weak"
            and signal != "NEUTRAL"
        ):
            adx_filter_reason = (
                f" [ADX_TREND_FILTER: ADX={adx14:.1f} < {_env_float('MIN_ADX', 20.0):.0f}"
                " → mercado lateral, sinal suprimido → NEUTRAL]"
            )
            signal = "NEUTRAL"

        majority_votes = max(bullish_votes, bearish_votes, neutral_votes)
        confidence = round((majority_votes / 3) * 100)
        shadow_signal, shadow_confidence, shadow_reason = _shadow_signal(votes)
        reasons = [
            f"vote1={votes[0]}",
            f"vote2={votes[1]}",
            f"vote3={votes[2]}",
            f"score={score:+.2f}",
        ]
        technical_reason = (
            f"role={role or 'default'}; RSI {rsi_signal}; EMA {ema_trend}; "
            f"MACD {macd_signal}; estrutura {structure}. "
            f"Score técnico ponderado={score:+.2f} "
            f"(RSI={weights['rsi']:.2f}, EMA={weights['ema']:.2f}, MACD={weights['macd']:.2f}); "
            f"sinal técnico é {signal}.{adx_filter_reason}"
        )

        return {
            "signal": signal,
            "confidence": confidence,
            "shadow_technical_signal": shadow_signal,
            "shadow_technical_confidence": shadow_confidence,
            "shadow_technical_reason": shadow_reason,
            "technical_reason": technical_reason,
            "indicators": {
                "rsi": _round_or_none(rsi, 1),
                "rsi_signal": rsi_signal,
                "rsi_vote": rsi_signal,
                "ema20": _round_or_none(ema20),
                "ema50": _round_or_none(ema50),
                "ema200": _round_or_none(ema200),
                "ema_trend": ema_trend,
                "ema_vote": ema_trend,
                "macd": _round_or_none(macd_value, 4),
                "macd_signal": macd_signal,
                "macd_signal_value": _round_or_none(macd_signal_value, 4),
                "macd_vote": macd_signal,
                "macd_minus_signal": _round_or_none(macd_delta, 5),
                "adx": _round_or_none(adx14, 1),
                "adx14": _round_or_none(adx14, 1),
                "adx_vote": adx_vote,
                "atr14": _round_or_none(atr14, 4),
                "atr_price": _round_or_none(atr14, 4),
                "atr_pips": _round_or_none(atr_pips, 1),
                "volatility_reason": _atr_reason(_round_or_none(atr_pips, 1)),
                "current_price": _round_or_none(current_price, 4),
                "ema20_minus_ema50": _round_or_none(ema_delta, 5),
                "structure": structure,
                "volume_spike": volume_spike,
                "technical_score": _round_or_none(score, 4),
                "technical_signal": signal,
                "technical_score_reasons": reasons,
            },
        }

    except Exception as e:
        print(f"[technical] Erro na análise técnica de {pair}: {e}")
        return _neutral_result()
