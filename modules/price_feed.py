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


def fetch_candles(pair="EUR/USD", timeframe="1h", count=100):
    try:
        ticker = _pair_to_yahoo_ticker(pair)
        df = yf.download(
            ticker,
            period="7d",
            interval=timeframe,
            progress=False,
            threads=False,
        )

        if df.empty:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        df = _normalise_columns(df)
        return df.tail(count)

    except Exception as e:
        print(f"[price_feed] Erro ao ler candles para {pair}: {e}")
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])


def get_current_price(pair="EUR/USD"):
    candles = fetch_candles(pair=pair, count=1)
    if candles.empty:
        return None
    return float(candles["close"].iloc[-1])
