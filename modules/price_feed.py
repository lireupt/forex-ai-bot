import pandas as pd
import yfinance as yf

PROVIDER = "yahoo"


def _pair_to_yahoo_ticker(pair):
    base, quote = pair.replace(" ", "").upper().split("/")
    return f"{base}{quote}=X"


def _normalise_columns(df):
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [
            next((part for part in col if str(part).lower() in {"open", "high", "low", "close", "volume"}), col[0])
            for col in df.columns
        ]

    rename_map = {
        "Open": "open",
        "High": "high",
        "Low": "low",
        "Close": "close",
        "Adj Close": "close",
        "Volume": "volume",
    }
    df = df.rename(columns=rename_map)

    columns = ["open", "high", "low", "close", "volume"]
    for column in columns:
        if column not in df.columns:
            df[column] = 0

    return df[columns].dropna(subset=["open", "high", "low", "close"])


def _download_yahoo(ticker, timeframe):
    period_by_timeframe = {
        "15m": "30d",
        "1h": "60d",
        "60m": "60d",
        "4h": "60d",
        "1d": "1y",
    }
    interval = "1h" if timeframe == "4h" else timeframe
    return yf.download(
        ticker,
        period=period_by_timeframe.get(timeframe, "7d"),
        interval=interval,
        progress=False,
        threads=False,
    )


def _resample_4h(df):
    if df.empty:
        return df
    if not isinstance(df.index, pd.DatetimeIndex):
        return df
    agg = {
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }
    return df.resample("4h").agg(agg).dropna(subset=["open", "high", "low", "close"])


def fetch_candles(pair="EUR/USD", timeframe="1h", count=100):
    try:
        ticker = _pair_to_yahoo_ticker(pair)
        df = _download_yahoo(ticker, timeframe)

        if df.empty:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        df = _normalise_columns(df)
        if timeframe == "4h":
            df = _resample_4h(df)
        return df.tail(count)

    except Exception as e:
        print(f"[price_feed] Erro ao ler candles para {pair}: {e}")
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])


def get_current_price(pair="EUR/USD"):
    candles = fetch_candles(pair=pair, count=1)
    if candles.empty:
        return None
    return float(candles["close"].iloc[-1])
