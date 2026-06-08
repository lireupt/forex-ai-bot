"""Monitorização quase em tempo real das operações abertas.

Verifica continuamente os paper-trades com status='open' e fecha-os em
tempo real assim que o preço atual toca no SL ou TP, sem aguardar a
chegada de novas candles horárias.

Uso:
    python monitor_trades.py

Configuração (.env):
    TRADE_MONITOR_INTERVAL_SECONDS=30
"""

import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

from modules import database
from modules.realtime_price import get_price as get_current_price

load_dotenv()

PIP_SIZE = 0.0001
_DEFAULT_INTERVAL = 30


def _env_int(name, default):
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    try:
        return int(float(value))
    except ValueError:
        return default


def _utc_now():
    return datetime.now(timezone.utc)


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


# ---------------------------------------------------------------------------
# Price fetching — one request per symbol per cycle
# ---------------------------------------------------------------------------

def fetch_prices_for_trades(trades):
    """Devolve {pair: price} para todos os pares nas trades, sem pedidos duplicados."""
    pairs = {t["pair"] for t in trades}
    prices = {}
    for pair in pairs:
        try:
            price = get_current_price(pair)
            if price is not None:
                prices[pair] = price
            else:
                print(f"[MONITOR] Aviso: preço indisponível para {pair}")
        except Exception as exc:
            print(f"[MONITOR] Erro ao obter preço de {pair}: {exc}")
    return prices


# ---------------------------------------------------------------------------
# SL/TP evaluation
# ---------------------------------------------------------------------------

def _evaluate_sltp(trade, current_price):
    """Avalia se a trade atingiu SL ou TP. Devolve ('win'|'loss', close_price) ou None."""
    direction = trade["direction"].upper()
    sl = float(trade["simulated_sl"])
    tp = float(trade["simulated_tp"])

    if direction == "BUY":
        if current_price <= sl:
            return "loss", sl
        if current_price >= tp:
            return "win", tp
    elif direction == "SELL":
        if current_price >= sl:
            return "loss", sl
        if current_price <= tp:
            return "win", tp

    return None


def _signed_pips(direction, entry, close_price):
    delta = close_price - entry
    if direction.upper() == "SELL":
        delta = -delta
    return round(delta / PIP_SIZE, 1)


def _r_multiple(direction, entry, close_price, sl):
    risk = abs(entry - sl)
    if risk == 0:
        return None
    delta = close_price - entry
    if direction.upper() == "SELL":
        delta = -delta
    return round(delta / risk, 2)


def _compute_distances(trade, current_price):
    """Distância em pips ao TP e ao SL. Positivo = ainda não atingido."""
    direction = trade["direction"].upper()
    tp = float(trade["simulated_tp"])
    sl = float(trade["simulated_sl"])
    if direction == "BUY":
        dist_tp = round((tp - current_price) / PIP_SIZE, 1)
        dist_sl = round((current_price - sl) / PIP_SIZE, 1)
    else:
        dist_tp = round((current_price - tp) / PIP_SIZE, 1)
        dist_sl = round((sl - current_price) / PIP_SIZE, 1)
    return dist_tp, dist_sl


def _duration_str(created_at_iso, closed_at_dt):
    opened = _parse_iso(created_at_iso)
    if opened is None:
        return "?"
    delta = closed_at_dt - opened
    total_seconds = int(delta.total_seconds())
    hours, remainder = divmod(abs(total_seconds), 3600)
    minutes = remainder // 60
    return f"{hours}h{minutes:02d}m"


# ---------------------------------------------------------------------------
# Close trade
# ---------------------------------------------------------------------------

def close_trade(conn, trade, result, close_price):
    now = _utc_now()
    entry = float(trade["entry_price"])
    sl = float(trade["simulated_sl"])
    direction = trade["direction"].upper()

    pips = _signed_pips(direction, entry, close_price)
    r_mult = _r_multiple(direction, entry, close_price, sl)
    duration = _duration_str(trade.get("created_at"), now)
    closed_at_iso = now.isoformat()

    close_reason = "TP atingido (monitor)" if result == "win" else "SL atingido (monitor)"

    database.update_paper_trade_result(
        conn,
        paper_trade_id=trade["id"],
        status=result,
        close_price=round(close_price, 5),
        closed_at=closed_at_iso,
        close_reason=close_reason,
        result_pips=pips,
        result_r_multiple=r_mult,
    )

    print(
        f"[MONITOR]\n"
        f"  Trade #{trade['id']}\n"
        f"  Symbol: {trade['pair']}\n"
        f"  Direction: {direction}\n"
        f"  Current Price: {close_price:.5f}\n"
        f"  {'TP' if result == 'win' else 'SL'}: {close_price:.5f}\n"
        f"  Result: {'WIN' if result == 'win' else 'LOSS'}\n"
        f"  Pips: {pips:+.1f} | R: {r_mult}\n"
        f"  Duration: {duration}"
    )


# ---------------------------------------------------------------------------
# One monitoring cycle
# ---------------------------------------------------------------------------

def run_cycle(conn):
    open_trades = database.get_open_paper_trades(conn)

    if not open_trades:
        return 0

    prices = fetch_prices_for_trades(open_trades)
    closed = 0

    for trade in open_trades:
        pair = trade["pair"]
        current_price = prices.get(pair)

        if current_price is None:
            continue

        # Atualizar preço e distâncias mesmo sem atingir SL/TP
        dist_tp, dist_sl = _compute_distances(trade, current_price)
        try:
            database.update_paper_trade_monitor_price(
                conn,
                trade["id"],
                current_price,
                _utc_now().isoformat(),
                dist_tp,
                dist_sl,
            )
        except Exception as exc:
            print(f"[MONITOR] Erro ao atualizar preço da trade #{trade['id']}: {exc}")

        # Avaliar SL/TP
        evaluation = _evaluate_sltp(trade, current_price)
        if evaluation is None:
            continue

        result, close_price = evaluation
        try:
            close_trade(conn, trade, result, close_price)
            closed += 1
        except Exception as exc:
            print(f"[MONITOR] Erro ao fechar trade #{trade['id']}: {exc}")

    return closed


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    interval = _env_int("TRADE_MONITOR_INTERVAL_SECONDS", _DEFAULT_INTERVAL)
    print(f"[MONITOR] Iniciado — intervalo: {interval}s")

    conn = database.connect()
    database.init_db(conn)

    while True:
        try:
            open_trades = database.get_open_paper_trades(conn)

            if not open_trades:
                time.sleep(interval)
                continue

            print(
                f"[MONITOR] {_utc_now().strftime('%Y-%m-%d %H:%M:%S')} UTC — "
                f"{len(open_trades)} trade(s) em aberto"
            )
            closed = run_cycle(conn)
            if closed:
                print(f"[MONITOR] {closed} trade(s) fechada(s) neste ciclo")

        except Exception as exc:
            print(f"[MONITOR] Erro inesperado no ciclo: {exc}")

        time.sleep(interval)


if __name__ == "__main__":
    main()
