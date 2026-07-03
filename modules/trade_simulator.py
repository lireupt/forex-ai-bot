"""Simulação pura de resolução de uma trade — sem I/O, sem acesso a DB.

Percorre candles cronológicas entre a abertura e a expiry de uma trade e
determina se/quando SL ou TP foram tocados, aplicando a regra conservadora
de assumir SL primeiro quando ambos são tocados na mesma candle. Usado
tanto pelo avaliador live (`scripts/evaluate_paper_trades.py`, sem spread)
como pelo motor de backtest (Fase A, com spread configurável).
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


@dataclass(frozen=True)
class TradeResult:
    status: str  # "win", "loss" ou "expired"
    close_price: float
    closed_at: str
    close_reason: str
    result_pips: float
    result_r_multiple: Optional[float]


def _parse_iso(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def signed_pips(direction, entry, exit_price, pip_size):
    delta = exit_price - entry
    if direction == "SELL":
        delta = -delta
    return round(delta / pip_size, 1)


def compute_r_multiple(direction, entry, exit_price, sl_price):
    risk = abs(entry - sl_price)
    if risk == 0:
        return None
    delta = exit_price - entry
    if direction == "SELL":
        delta = -delta
    return round(delta / risk, 2)


def simulate_trade(trade, candles, pair_spec, apply_spread=False, now_dt=None):
    """Resolve uma trade contra uma sequência de candles cronológicas.

    `now_dt` é o instante de "agora" usado para decidir expiry — no avaliador
    live é o wall-clock real (default); no backtest é o `t` simulado, para
    respeitar point-in-time estrito.
    """
    direction = trade["direction"]
    entry = float(trade["entry_price"])
    sl = float(trade["simulated_sl"])
    tp = float(trade["simulated_tp"])

    if apply_spread:
        spread = pair_spec.spread_pips * pair_spec.pip_size
        entry = entry + spread if direction == "BUY" else entry - spread

    expiry_dt = _parse_iso(trade.get("expiry_at"))
    now = now_dt or datetime.now(timezone.utc)

    for candle in candles:
        candle_time = _parse_iso(candle["candle_time"])
        if candle_time is None:
            continue
        high = float(candle["high"])
        low = float(candle["low"])

        hit_tp = (direction == "BUY" and high >= tp) or (direction == "SELL" and low <= tp)
        hit_sl = (direction == "BUY" and low <= sl) or (direction == "SELL" and high >= sl)

        if hit_tp and hit_sl:
            close_price = sl
            status = "loss"
            reason = "SL e TP na mesma candle — assumido SL primeiro"
        elif hit_tp:
            close_price = tp
            status = "win"
            reason = "TP atingido"
        elif hit_sl:
            close_price = sl
            status = "loss"
            reason = "SL atingido"
        else:
            continue

        return TradeResult(
            status=status,
            close_price=round(close_price, 5),
            closed_at=candle_time.isoformat(),
            close_reason=reason,
            result_pips=signed_pips(direction, entry, close_price, pair_spec.pip_size),
            result_r_multiple=compute_r_multiple(direction, entry, close_price, sl),
        )

    if expiry_dt is not None and expiry_dt <= now:
        last_close = float(candles[-1]["close"]) if candles else entry
        return TradeResult(
            status="expired",
            close_price=round(last_close, 5),
            closed_at=expiry_dt.isoformat(),
            close_reason="expirou sem atingir SL/TP",
            result_pips=signed_pips(direction, entry, last_close, pair_spec.pip_size),
            result_r_multiple=compute_r_multiple(direction, entry, last_close, sl),
        )

    return None
