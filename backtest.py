"""Backtest do sinal técnico (estrito + shadow + score-based) sobre candles
históricas.

Usa a mesma `analyse_technical` do live para que o backtest não divirja da
produção. Para cada barra com indicadores válidos, regista os sinais e
calcula os pips após N barras (1h/4h/24h).

Inclui agora o `score-based combined`: como não temos histórico de AI score
nem podemos re-gerar AI a cada candle, usamos `ai_score=0` (neutral) e o
`combined_score` cai em cima do `technical_score` (com peso default 0.4).
Útil para isolar o que a técnica + threshold dariam.

Uso:
    python backtest.py                       # 720 candles 1h, EUR/USD
    python backtest.py --period 60d --tf 1h
    python backtest.py --pair GBP/USD --period 30d
"""

import argparse
import sys
from collections import defaultdict

import pandas as pd
import yfinance as yf

from modules import scoring
from modules.technical import analyse as analyse_technical

PIP_SIZE = 0.0001
WARMUP_BARS = 50  # EMA50 precisa de pelo menos 50 candles


def _pair_to_yahoo_ticker(pair):
    base, quote = pair.replace(" ", "").upper().split("/")
    return f"{base}{quote}=X"


def _normalise_columns(df):
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [
            next(
                (part for part in col if str(part).lower() in {"open", "high", "low", "close", "volume"}),
                col[0],
            )
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


def fetch_history(pair, timeframe, period):
    ticker = _pair_to_yahoo_ticker(pair)
    df = yf.download(
        ticker,
        period=period,
        interval=timeframe,
        progress=False,
        threads=False,
    )
    if df.empty:
        return df
    return _normalise_columns(df)


def _bars_per_horizon(timeframe):
    base = {
        "1h": {"1h": 1, "4h": 4, "24h": 24},
        "30m": {"1h": 2, "4h": 8, "24h": 48},
        "15m": {"1h": 4, "4h": 16, "24h": 96},
        "1d": {"1h": 1, "4h": 1, "24h": 1},
    }
    return base.get(timeframe, base["1h"])


def _signed_pips(signal, entry, future):
    if signal not in ("BUY", "SELL"):
        return None
    delta = future - entry
    if signal == "SELL":
        delta = -delta
    return delta / PIP_SIZE


def _score_signal_for_window(result, scoring_config):
    """Calcula o score combinado e devolve (signal, score) usando AI=0
    (neutral) e a técnica avaliada na window. Reflete o que o threshold
    produziria se a IA não inclinasse o resultado."""
    indicators = result.get("indicators", {})
    technical_score = scoring.technical_votes_score(
        indicators.get("rsi_vote", indicators.get("rsi_signal", "neutral")),
        indicators.get("ema_vote", indicators.get("ema_trend", "neutral")),
        indicators.get("macd_vote", indicators.get("macd_signal", "neutral")),
    )
    shadow_score = scoring.signal_score(
        result.get("shadow_technical_signal"),
        result.get("shadow_technical_confidence"),
    )
    combined_score = scoring.combine_scores(
        ai_score=0.0,
        technical_score=technical_score,
        shadow_score=shadow_score,
        config=scoring_config,
    )
    return scoring.score_to_signal(combined_score, scoring_config), combined_score


def run_backtest(pair, timeframe, period):
    df = fetch_history(pair, timeframe, period)
    if df.empty:
        print(f"Sem candles para {pair} ({timeframe}, {period}).")
        return

    total_bars = len(df)
    if total_bars < WARMUP_BARS + 25:
        print(f"Apenas {total_bars} candles — insuficiente para backtest.")
        return

    horizons = _bars_per_horizon(timeframe)
    max_horizon_bars = max(horizons.values())
    scoring_config = scoring.load_scoring_config()

    signals = {"strict": [], "shadow": [], "score": []}

    for i in range(WARMUP_BARS, total_bars - max_horizon_bars):
        window = df.iloc[: i + 1]
        result = analyse_technical(window, pair=pair)
        indicators = result.get("indicators", {})
        entry = indicators.get("current_price")
        if entry is None:
            continue

        score_signal, _ = _score_signal_for_window(result, scoring_config)

        triplets = (
            ("strict", result.get("signal")),
            ("shadow", result.get("shadow_technical_signal")),
            ("score", score_signal),
        )

        for source_key, signal in triplets:
            if signal not in ("BUY", "SELL"):
                continue
            outcomes = {}
            for horizon, bars in horizons.items():
                future_idx = i + bars
                if future_idx >= total_bars:
                    continue
                future_price = float(df.iloc[future_idx]["close"])
                outcomes[horizon] = _signed_pips(signal, entry, future_price)
            signals[source_key].append({
                "bar": i,
                "time": df.index[i],
                "signal": signal,
                "entry": entry,
                "outcomes": outcomes,
            })

    _print_summary(pair, timeframe, period, total_bars, signals, scoring_config)


def _summarise(entries, horizon):
    pips = [e["outcomes"].get(horizon) for e in entries if e["outcomes"].get(horizon) is not None]
    if not pips:
        return None
    wins = sum(1 for p in pips if p > 0)
    losses = sum(1 for p in pips if p < 0)
    avg = sum(pips) / len(pips)
    return {
        "count": len(pips),
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / len(pips) * 100, 1),
        "avg_pips": round(avg, 1),
        "total_pips": round(sum(pips), 1),
    }


def _print_summary(pair, timeframe, period, total_bars, signals, scoring_config):
    print()
    print(f"=== Backtest {pair} {timeframe} ({period}) ===")
    print(f"Candles analisadas: {total_bars}")
    print(f"Bar warmup: {WARMUP_BARS}")
    print(
        f"Score thresholds: BUY>={scoring_config['buy_threshold']:+.2f} "
        f"SELL<={scoring_config['sell_threshold']:+.2f} "
        f"(pesos AI={scoring_config['ai_weight']:.2f} tech={scoring_config['technical_weight']:.2f})"
    )
    print()

    label_map = (
        ("strict", "Estrito (3/3)"),
        ("shadow", "Shadow (2/3)"),
        ("score", "Score-based (AI=0, técnica)"),
    )
    for source_key, label in label_map:
        entries = signals.get(source_key, [])
        by_signal = defaultdict(list)
        for e in entries:
            by_signal[e["signal"]].append(e)

        total = len(entries)
        print(f"--- {label} ---")
        if total == 0:
            print("  Sem sinais BUY/SELL.")
            print()
            continue

        print(f"  Sinais totais: {total}  (BUY={len(by_signal['BUY'])}, SELL={len(by_signal['SELL'])})")
        for horizon in ("1h", "4h", "24h"):
            stats = _summarise(entries, horizon)
            if stats is None:
                continue
            print(
                f"  {horizon}: "
                f"{stats['wins']} W / {stats['losses']} L / {stats['count']} total "
                f"({stats['win_rate']}%) "
                f"avg={stats['avg_pips']} pips, total={stats['total_pips']} pips"
            )
        print()


def parse_args():
    parser = argparse.ArgumentParser(description="Backtest do sinal técnico estrito e shadow.")
    parser.add_argument("--pair", default="EUR/USD")
    parser.add_argument("--tf", dest="timeframe", default="1h",
                        help="timeframe yfinance (ex: 1h, 30m, 15m, 1d)")
    parser.add_argument("--period", default="60d",
                        help="período yfinance (ex: 7d, 60d, 6mo, 1y, 2y)")
    return parser.parse_args()


def main():
    args = parse_args()
    try:
        run_backtest(args.pair, args.timeframe, args.period)
    except KeyboardInterrupt:
        print("\nInterrompido.")
        sys.exit(1)


if __name__ == "__main__":
    main()
