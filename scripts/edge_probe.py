"""Backtest vetorizado do sinal técnico para diagnosticar o edge.

Replica fielmente a lógica de votos de modules/technical.py (roles default e h1)
mas calcula os indicadores uma vez (O(n)) para correr sobre anos de candles.
Quebra resultados por direção, regime ADX, filtro EMA200 e sinal invertido.
"""
import sys
import numpy as np
import pandas as pd
import pandas_ta as ta
import yfinance as yf

PIP = 0.0001
HORIZONS = {"1h": 1, "4h": 4, "24h": 24}


def load(tf="1h", ticker="EURUSD=X"):
    # yfinance não tem 4h nativo: puxa 1h e reamostra. 1d vai direto (período longo).
    if tf == "1h":
        raw = yf.download(ticker, period="2y", interval="1h", progress=False, threads=False)
    elif tf == "4h":
        raw = yf.download(ticker, period="2y", interval="1h", progress=False, threads=False)
    elif tf == "1d":
        raw = yf.download(ticker, period="10y", interval="1d", progress=False, threads=False)
    else:
        raise SystemExit(f"tf não suportado: {tf}")
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = [c[0] for c in raw.columns]
    df = raw.rename(columns=str.lower)[["open", "high", "low", "close"]].dropna()
    if tf == "4h":
        df = df.resample("4h").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    return df


def indicators(df):
    c, h, l = df["close"], df["high"], df["low"]
    df["rsi"] = ta.rsi(c, length=14)
    df["ema20"] = ta.ema(c, length=20)
    df["ema50"] = ta.ema(c, length=50)
    df["ema200"] = ta.ema(c, length=200)
    df["atr"] = ta.atr(h, l, c, length=14)
    adx = ta.adx(h, l, c, length=14)
    df["adx"] = adx["ADX_14"] if adx is not None else np.nan
    macd = ta.macd(c, fast=12, slow=26, signal=9)
    df["macd"] = macd["MACD_12_26_9"]
    df["macds"] = macd["MACDs_12_26_9"]
    # votos (mesma lógica de technical.py)
    df["rsi_vote"] = np.where(df["rsi"] < 35, 1.0, np.where(df["rsi"] > 65, -1.0, 0.0))
    df["ema_vote"] = np.where(df["ema20"] > df["ema50"], 1.0, -1.0)
    df["macd_vote"] = np.where(df["macd"] > df["macds"], 1.0, -1.0)
    # estrutura: compara high/low actual vs 5 barras atrás (tail(6))
    hh, ll = h > h.shift(5), l > l.shift(5)
    lh, lll = h < h.shift(5), l < l.shift(5)
    df["struct_vote"] = np.where(hh & ll, 1.0, np.where(lh & lll, -1.0, 0.0))
    return df.dropna()


def score(v0, v1, v2):
    # pesos posicionais como em technical._technical_score (rsi=.30 ema=.40 macd=.30)
    return (v0 * 0.30 + v1 * 0.40 + v2 * 0.30) / 1.0


def signal_from_votes(df, role):
    if role == "h1":
        v0, v1, v2 = df["ema_vote"], df["macd_vote"], df["struct_vote"]
    else:  # default
        v0, v1, v2 = df["rsi_vote"], df["ema_vote"], df["macd_vote"]
    s = score(v0, v1, v2)
    sig = np.where(s >= 0.35, "BUY", np.where(s <= -0.35, "SELL", "NEUTRAL"))
    return pd.Series(sig, index=df.index), s


def fwd_pips(df, bars, sig):
    fut = df["close"].shift(-bars)
    delta = (fut - df["close"]) / PIP
    return np.where(sig == "SELL", -delta, delta)


def stats(pips):
    pips = pips[~np.isnan(pips)]
    if len(pips) == 0:
        return None
    wins = (pips > 0).sum()
    gross_w = pips[pips > 0].sum()
    gross_l = -pips[pips < 0].sum()
    pf = gross_w / gross_l if gross_l > 0 else float("inf")
    return dict(n=len(pips), wr=100 * wins / len(pips), avg=pips.mean(), total=pips.sum(), pf=pf)


def show(title, df, sig, mask=None):
    print(f"\n--- {title} ---")
    m = mask if mask is not None else pd.Series(True, index=df.index)
    for label, dirmask in (("BUY", sig == "BUY"), ("SELL", sig == "SELL"), ("AMBOS", sig.isin(["BUY", "SELL"]))):
        sel = m & dirmask
        if sel.sum() == 0:
            continue
        parts = []
        for hz, bars in HORIZONS.items():
            st = stats(fwd_pips(df, bars, sig)[sel.values])
            if st:
                parts.append(f"{hz}: {st['wr']:.0f}% avg{st['avg']:+.1f} PF{st['pf']:.2f} (n={st['n']})")
        print(f"  {label:5s} {' | '.join(parts)}")


def probe_tf(tf):
    df = indicators(load(tf))
    print(f"\n############ EUR/USD {tf} — {len(df)} candles ({df.index.min().date()} a {df.index.max().date()}) ############")
    print(f"(horizontes 1/4/24 barras = {tf} x 1/4/24; win = close à frente a favor; ignora spread)")
    sig, _ = signal_from_votes(df, "default")
    show("role default (RSI+EMA+MACD)", df, sig)
    sigh, _ = signal_from_votes(df, "h1")
    show("role h1 (EMA+MACD+estrutura, = produção)", df, sigh)
    show("FILTRO ADX<20 (lateral) sobre default", df, sig, mask=df["adx"] < 20)


def main():
    tfs = sys.argv[1].split(",") if len(sys.argv) > 1 else ["1h", "4h", "1d"]
    for tf in tfs:
        probe_tf(tf)


if __name__ == "__main__":
    main()
