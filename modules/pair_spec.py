"""Especificação de par: pip size, spread e parâmetros de paper-trade.

Fonte única de verdade para constantes por-par que hoje estão hardcoded em
`main.py`, `backtest.py` e `scripts/evaluate_paper_trades.py` (PIP_SIZE,
multiplicadores de SL/TP, expiry em barras).
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class PairSpec:
    pair: str
    pip_size: float
    spread_pips: float
    sl_atr_mult: float
    tp_atr_mult: float
    expiry_bars: int


_REGISTRY = {
    "EUR/USD": PairSpec(
        pair="EUR/USD",
        pip_size=0.0001,
        spread_pips=1.0,
        sl_atr_mult=1.0,
        tp_atr_mult=2.0,
        expiry_bars=6,
    ),
}


def get_pair_spec(pair):
    try:
        return _REGISTRY[pair]
    except KeyError:
        raise KeyError(f"Sem PairSpec registada para o par '{pair}'")
