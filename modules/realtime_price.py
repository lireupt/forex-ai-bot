"""Preço em tempo real para o monitor de trades.

Ordem de prioridade:
  1. Twelve Data  — se TWELVEDATA_API_KEY estiver definida
  2. yfinance     — fallback sempre disponível

Uso:
    from modules.realtime_price import get_price
    price = get_price("EUR/USD")   # float ou None
"""

import os

import requests

_TWELVEDATA_URL = "https://api.twelvedata.com/price"
_TIMEOUT_SECONDS = 8


def _twelvedata_key():
    key = os.getenv("TWELVEDATA_API_KEY", "").strip()
    return key if key and key.upper() not in {"", "YOUR_KEY_HERE", "PLACEHOLDER"} else None


def _get_via_twelvedata(pair):
    """Devolve float ou lança excepção."""
    key = _twelvedata_key()
    if not key:
        raise RuntimeError("TWELVEDATA_API_KEY não configurada")

    # Twelve Data aceita "EUR/USD" directamente
    resp = requests.get(
        _TWELVEDATA_URL,
        params={"symbol": pair, "apikey": key},
        timeout=_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    data = resp.json()

    # {"price": "1.08456"} em sucesso
    # {"code": 400, "message": "..."} em erro
    if "price" not in data:
        raise RuntimeError(f"Twelve Data erro: {data.get('message', data)}")

    return float(data["price"])


def _get_via_yfinance(pair):
    """Fallback — usa o módulo price_feed existente."""
    from modules.price_feed import get_current_price
    price = get_current_price(pair)
    if price is None:
        raise RuntimeError(f"yfinance não devolveu preço para {pair}")
    return price


def get_price(pair):
    """Devolve o preço atual do par. Tenta Twelve Data; cai para yfinance se falhar."""
    key = _twelvedata_key()

    if key:
        try:
            price = _get_via_twelvedata(pair)
            return price
        except Exception as exc:
            print(f"[realtime_price] Twelve Data falhou para {pair}: {exc} — fallback yfinance")

    try:
        return _get_via_yfinance(pair)
    except Exception as exc:
        print(f"[realtime_price] yfinance falhou para {pair}: {exc}")
        return None


def check_twelvedata_connectivity(pair="EUR/USD"):
    """Testa a conectividade com Twelve Data. Devolve dict com resultado."""
    key = _twelvedata_key()
    if not key:
        return {
            "ok": False,
            "reason": "TWELVEDATA_API_KEY não está definida no .env",
            "price": None,
        }
    try:
        price = _get_via_twelvedata(pair)
        return {"ok": True, "reason": "ok", "price": price}
    except Exception as exc:
        return {"ok": False, "reason": str(exc), "price": None}
