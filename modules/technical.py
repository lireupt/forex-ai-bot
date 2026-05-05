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
            "ema_trend": "neutral",
            "ema_vote": "neutral",
            "macd": None,
            "macd_signal": "neutral",
            "macd_signal_value": None,
            "macd_vote": "neutral",
            "atr14": None,
            "atr_price": None,
            "atr_pips": None,
            "volatility_reason": "ATR indisponível.",
            "current_price": None,
        },
    }


def _round_or_none(value, digits=4):
    if pd.isna(value):
        return None
    return round(float(value), digits)


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


def analyse(candles_df, pair="EUR/USD"):
    try:
        if candles_df is None or candles_df.empty:
            return _neutral_result()

        df = candles_df.copy()
        if "close" not in df.columns:
            return _neutral_result()

        df["RSI_14"] = ta.rsi(df["close"], length=14)
        df["EMA_20"] = ta.ema(df["close"], length=20)
        df["EMA_50"] = ta.ema(df["close"], length=50)
        df["ATR_14"] = ta.atr(df["high"], df["low"], df["close"], length=14)

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
        usable = df.dropna(subset=required)
        if usable.empty:
            return _neutral_result()

        last = usable.iloc[-1]
        rsi = float(last["RSI_14"])
        ema20 = float(last["EMA_20"])
        ema50 = float(last["EMA_50"])
        macd_value = float(last["MACD_12_26_9"])
        macd_signal_value = float(last["MACDs_12_26_9"])
        atr14 = float(last["ATR_14"])
        atr_pips = atr14 / PIP_SIZE
        current_price = float(last["close"])

        if rsi < 35:
            rsi_signal = "bullish"
        elif rsi > 65:
            rsi_signal = "bearish"
        else:
            rsi_signal = "neutral"

        ema_trend = "bullish" if ema20 > ema50 else "bearish"
        macd_signal = "bullish" if macd_value > macd_signal_value else "bearish"

        votes = [rsi_signal, ema_trend, macd_signal]
        bullish_votes = votes.count("bullish")
        bearish_votes = votes.count("bearish")
        neutral_votes = votes.count("neutral")

        if bullish_votes == 3:
            signal = "BUY"
        elif bearish_votes == 3:
            signal = "SELL"
        else:
            signal = "NEUTRAL"

        majority_votes = max(bullish_votes, bearish_votes, neutral_votes)
        confidence = round((majority_votes / 3) * 100)
        shadow_signal, shadow_confidence, shadow_reason = _shadow_signal(votes)
        technical_reason = (
            f"RSI {rsi_signal}; EMA {ema_trend}; MACD {macd_signal}. "
            f"BUY/SELL exige 3 votos alinhados, por isso o sinal técnico é {signal}."
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
                "ema_trend": ema_trend,
                "ema_vote": ema_trend,
                "macd": _round_or_none(macd_value, 4),
                "macd_signal": macd_signal,
                "macd_signal_value": _round_or_none(macd_signal_value, 4),
                "macd_vote": macd_signal,
                "atr14": _round_or_none(atr14, 4),
                "atr_price": _round_or_none(atr14, 4),
                "atr_pips": _round_or_none(atr_pips, 1),
                "volatility_reason": _atr_reason(_round_or_none(atr_pips, 1)),
                "current_price": _round_or_none(current_price, 4),
            },
        }

    except Exception as e:
        print(f"[technical] Erro na análise técnica de {pair}: {e}")
        return _neutral_result()
