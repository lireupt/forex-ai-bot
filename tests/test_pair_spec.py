"""Testes para `modules.pair_spec` — registo de PairSpec por par."""

import pytest

from modules.pair_spec import PairSpec, get_pair_spec


class TestGetPairSpec:
    def test_eurusd_spec_matches_production_constants(self):
        spec = get_pair_spec("EUR/USD")
        assert spec.pair == "EUR/USD"
        assert spec.pip_size == 0.0001
        assert spec.spread_pips == 1.0
        assert spec.sl_atr_mult == 1.0
        assert spec.tp_atr_mult == 2.0
        assert spec.expiry_bars == 6

    def test_unknown_pair_raises_key_error(self):
        with pytest.raises(KeyError):
            get_pair_spec("GBP/USD")

    def test_spec_is_frozen(self):
        spec = get_pair_spec("EUR/USD")
        with pytest.raises(Exception):
            spec.pip_size = 0.01

    def test_spec_is_dataclass_instance(self):
        assert isinstance(get_pair_spec("EUR/USD"), PairSpec)
