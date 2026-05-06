"""Testes para os helpers em `main.py` que fazem cálculos não triviais.

Foco: `_build_paper_trade`, `_volatility_label`, `_build_features_snapshot`,
`_build_combined_reason`, `_build_blocking_reason`, `_recent_change_pct`.
"""

from datetime import datetime, timezone

import pandas as pd
import pytest

import main


class TestVolatilityLabel:
    def test_low(self):
        assert main._volatility_label(5.0) == "low"

    def test_normal(self):
        assert main._volatility_label(15.0) == "normal"

    def test_high(self):
        assert main._volatility_label(25.0) == "high"

    def test_boundary_low(self):
        assert main._volatility_label(7.99) == "low"
        assert main._volatility_label(8.0) == "normal"

    def test_boundary_high(self):
        assert main._volatility_label(20.0) == "normal"
        assert main._volatility_label(20.01) == "high"

    def test_none_unknown(self):
        assert main._volatility_label(None) == "unknown"

    def test_invalid_unknown(self):
        assert main._volatility_label("foo") == "unknown"


class TestRecentChangePct:
    def _df(self, closes):
        idx = pd.date_range("2026-05-06", periods=len(closes), freq="1h")
        return pd.DataFrame(
            {
                "open": closes,
                "high": closes,
                "low": closes,
                "close": closes,
                "volume": [100] * len(closes),
            },
            index=idx,
        )

    def test_positive_change(self):
        df = self._df([1.0, 1.0, 1.0, 1.0, 1.05])
        # primeiro close=1, último=1.05 -> +5%
        assert main._recent_change_pct(df, n=5) == pytest.approx(5.0)

    def test_negative_change(self):
        df = self._df([1.0, 1.0, 1.0, 1.0, 0.95])
        assert main._recent_change_pct(df, n=5) == pytest.approx(-5.0)

    def test_empty_df_none(self):
        assert main._recent_change_pct(pd.DataFrame(), n=5) is None

    def test_single_row_none(self):
        df = self._df([1.0])
        assert main._recent_change_pct(df, n=5) is None


class TestBuildFeaturesSnapshot:
    def test_full_snapshot(self):
        technical_result = {
            "indicators": {
                "current_price": 1.1756,
                "rsi": 58.2,
                "ema20": 1.1747,
                "ema50": 1.1732,
                "macd": 0.0011,
                "macd_signal_value": 0.0013,
                "atr14": 0.0013,
                "atr_pips": 12.6,
                "ema_trend": "bullish",
                "macd_signal": "bearish",
            },
        }
        candles = pd.DataFrame(
            {
                "open": [1.17, 1.171],
                "high": [1.172, 1.173],
                "low": [1.169, 1.170],
                "close": [1.171, 1.172],
                "volume": [100, 100],
            },
            index=pd.date_range("2026-05-06", periods=2, freq="1h"),
        )
        ai_result = {
            "signal": "BUY", "confidence": 60, "risk_level": "MEDIUM",
            "hold_off": False,
        }
        snap = main._build_features_snapshot(technical_result, candles, ai_result)
        assert snap["close"] == 1.1756
        assert snap["rsi"] == 58.2
        assert snap["ema20_minus_ema50"] == pytest.approx(0.0015, abs=1e-9)
        assert snap["macd_minus_signal"] == pytest.approx(-0.0002, abs=1e-9)
        assert snap["volatility_level"] == "normal"
        assert snap["ai_signal"] == "BUY"
        assert snap["ai_confidence"] == 60
        assert len(snap["recent_candles"]) == 2

    def test_handles_missing_indicators(self):
        technical_result = {"indicators": {}}
        ai_result = {"signal": "NEUTRAL", "confidence": 0}
        snap = main._build_features_snapshot(technical_result, pd.DataFrame(), ai_result)
        assert snap["close"] is None
        assert snap["ema20_minus_ema50"] is None
        assert snap["macd_minus_signal"] is None
        assert snap["recent_candles"] == []
        assert snap["recent_change_pct"] is None


class TestAIObservability:
    def test_ai_reason_prefers_reasoning(self):
        reason = main._ai_reason(
            {"signal": "BUY", "confidence": 62, "reasoning": "Fundamental e técnico alinhados."},
            {"reasoning": "combined"},
        )
        assert reason == "Fundamental e técnico alinhados."

    def test_ai_reason_accepts_legacy_reason_field(self):
        reason = main._ai_reason({"signal": "SELL", "confidence": 55, "reason": "Dólar forte."})
        assert reason == "Dólar forte."

    def test_ai_reason_never_empty_for_new_decisions(self):
        reason = main._ai_reason({"signal": "NEUTRAL", "confidence": 0, "risk_level": "HIGH"})
        assert "NEUTRAL" in reason
        assert "raciocínio detalhado" in reason

    def test_ai_model_version_prefers_explicit_model(self):
        version = main._ai_model_version({"provider": "groq", "model_version": "groq:test-model"}, "claude")
        assert version == "groq:test-model"

    def test_ai_model_version_falls_back_to_provider(self):
        version = main._ai_model_version({"provider": "groq"}, "claude")
        assert version.startswith("groq:")


class TestBuildPaperTrade:
    def test_buy_with_atr(self):
        created_at = datetime(2026, 5, 6, 19, 0, tzinfo=timezone.utc)
        trade = main._build_paper_trade(
            decision_id=1, pair="EUR/USD", timeframe="1h", direction="BUY",
            current_price=1.1700, atr_pips=20.0, source="ai_only",
            signal_source="ai_signal", created_at_dt=created_at,
        )
        assert trade["direction"] == "BUY"
        assert trade["entry_price"] == 1.17
        # SL 1x ATR abaixo, TP 2x ATR acima (defaults)
        assert trade["sl_pips"] == 20.0
        assert trade["tp_pips"] == 40.0
        assert trade["simulated_sl"] == pytest.approx(1.168, abs=1e-5)
        assert trade["simulated_tp"] == pytest.approx(1.174, abs=1e-5)
        assert trade["status"] == "open"
        # expira 6h depois (default)
        assert trade["expiry_at"] == "2026-05-07T01:00:00+00:00"

    def test_sell_with_atr(self):
        created_at = datetime(2026, 5, 6, 19, 0, tzinfo=timezone.utc)
        trade = main._build_paper_trade(
            decision_id=1, pair="EUR/USD", timeframe="1h", direction="SELL",
            current_price=1.1700, atr_pips=20.0, source="combined",
            signal_source="combined_signal", created_at_dt=created_at,
        )
        assert trade["direction"] == "SELL"
        # SELL: SL acima, TP abaixo
        assert trade["simulated_sl"] == pytest.approx(1.172, abs=1e-5)
        assert trade["simulated_tp"] == pytest.approx(1.166, abs=1e-5)

    def test_neutral_returns_none(self):
        trade = main._build_paper_trade(
            decision_id=1, pair="EUR/USD", timeframe="1h", direction="NEUTRAL",
            current_price=1.17, atr_pips=20.0, source="ai_only",
            signal_source="ai_signal",
            created_at_dt=datetime.now(timezone.utc),
        )
        assert trade is None

    def test_no_price_returns_none(self):
        trade = main._build_paper_trade(
            decision_id=1, pair="EUR/USD", timeframe="1h", direction="BUY",
            current_price=None, atr_pips=20.0, source="ai_only",
            signal_source="ai_signal",
            created_at_dt=datetime.now(timezone.utc),
        )
        assert trade is None

    def test_atr_none_uses_fallback(self):
        created_at = datetime(2026, 5, 6, 19, 0, tzinfo=timezone.utc)
        trade = main._build_paper_trade(
            decision_id=1, pair="EUR/USD", timeframe="1h", direction="BUY",
            current_price=1.17, atr_pips=None, source="ai_only",
            signal_source="ai_signal", created_at_dt=created_at,
        )
        # fallback ATR = 15 pips -> SL 15 pips, TP 30 pips
        assert trade is not None
        assert trade["sl_pips"] == 15.0
        assert trade["tp_pips"] == 30.0

    def test_custom_multipliers_via_env(self, monkeypatch):
        monkeypatch.setenv("PAPER_TRADE_SL_MULT", "2.0")
        monkeypatch.setenv("PAPER_TRADE_TP_MULT", "3.0")
        monkeypatch.setenv("PAPER_TRADE_EXPIRY_BARS", "12")
        created_at = datetime(2026, 5, 6, 19, 0, tzinfo=timezone.utc)
        trade = main._build_paper_trade(
            decision_id=1, pair="EUR/USD", timeframe="1h", direction="BUY",
            current_price=1.17, atr_pips=10.0, source="combined",
            signal_source="combined_signal", created_at_dt=created_at,
        )
        assert trade["sl_pips"] == 20.0
        assert trade["tp_pips"] == 30.0
        # 12h depois
        assert trade["expiry_at"] == "2026-05-07T07:00:00+00:00"


class TestBuildBlockingReason:
    def test_uses_trade_block_reason_when_present(self):
        result = main._build_blocking_reason(
            combined={"signal": "BUY", "hold_off": False},
            trade_decision={"block_reason": "confiança baixa"},
        )
        assert result == "confiança baixa"

    def test_neutral_combined_when_no_block_reason(self):
        result = main._build_blocking_reason(
            combined={"signal": "NEUTRAL", "hold_off": False},
            trade_decision={"block_reason": None},
        )
        assert result == "sinal combinado é NEUTRAL"

    def test_hold_off_when_no_block_reason(self):
        result = main._build_blocking_reason(
            combined={"signal": "BUY", "hold_off": True},
            trade_decision={"block_reason": None},
        )
        assert result == "hold_off ativo"

    def test_empty_when_allowed(self):
        result = main._build_blocking_reason(
            combined={"signal": "BUY", "hold_off": False},
            trade_decision={"block_reason": None},
        )
        assert result == ""


class TestSelectGatingSignal:
    def _strict(self, signal="NEUTRAL", hold_off=False, agreement=False, confidence=0):
        return {
            "signal": signal, "hold_off": hold_off,
            "agreement": agreement, "confidence": confidence,
            "reasoning": "strict reason",
        }

    def _shadow(self, signal="BUY", confidence=60, reason="shadow agree"):
        return {"signal": signal, "confidence": confidence, "reason": reason}

    def test_default_is_strict(self):
        gating, mode = main._select_gating_signal(
            self._strict(signal="NEUTRAL"), "BUY", 0.5, self._shadow(), mode=None,
        )
        assert mode == "strict"
        assert gating["signal"] == "NEUTRAL"  # mantém o sinal estrito

    def test_strict_mode_preserves_combined(self):
        strict = self._strict(signal="BUY", confidence=80, agreement=True)
        gating, mode = main._select_gating_signal(strict, "SELL", -0.5, self._shadow(), mode="strict")
        assert mode == "strict"
        assert gating["signal"] == "BUY"
        assert gating["confidence"] == 80

    def test_score_mode_uses_score_signal(self):
        gating, mode = main._select_gating_signal(
            self._strict(signal="NEUTRAL"),
            score_signal="BUY", combined_score=0.5,
            shadow_combined=self._shadow(), mode="score",
        )
        assert mode == "score"
        assert gating["signal"] == "BUY"
        assert gating["confidence"] == 50  # |0.5|*100
        assert "gating=score" in gating["reasoning"]

    def test_score_mode_negative(self):
        gating, _ = main._select_gating_signal(
            self._strict(), score_signal="SELL", combined_score=-0.42,
            shadow_combined=self._shadow(), mode="score",
        )
        assert gating["signal"] == "SELL"
        assert gating["confidence"] == 42

    def test_shadow_mode_uses_shadow_combined(self):
        gating, mode = main._select_gating_signal(
            self._strict(signal="NEUTRAL"),
            score_signal="BUY", combined_score=0.5,
            shadow_combined=self._shadow(signal="BUY", confidence=70),
            mode="shadow",
        )
        assert mode == "shadow"
        assert gating["signal"] == "BUY"
        assert gating["confidence"] == 70
        assert "gating=shadow" in gating["reasoning"]

    def test_unknown_mode_falls_back_to_strict(self):
        gating, mode = main._select_gating_signal(
            self._strict(signal="BUY", confidence=90),
            score_signal="SELL", combined_score=-0.5,
            shadow_combined=self._shadow(), mode="banana",
        )
        assert mode == "strict"
        assert gating["signal"] == "BUY"

    def test_hold_off_propagates_in_score_mode(self):
        gating, _ = main._select_gating_signal(
            self._strict(signal="NEUTRAL", hold_off=True),
            score_signal="BUY", combined_score=0.6,
            shadow_combined=self._shadow(), mode="score",
        )
        # Mesmo em modo score, se a IA pede hold_off, mantém-se
        assert gating["hold_off"] is True

    def test_hold_off_propagates_in_shadow_mode(self):
        gating, _ = main._select_gating_signal(
            self._strict(signal="NEUTRAL", hold_off=True),
            score_signal="BUY", combined_score=0.6,
            shadow_combined=self._shadow(signal="BUY", confidence=70),
            mode="shadow",
        )
        assert gating["hold_off"] is True

    def test_score_mode_handles_none_score(self):
        gating, _ = main._select_gating_signal(
            self._strict(),
            score_signal=None, combined_score=None,
            shadow_combined=self._shadow(), mode="score",
        )
        assert gating["signal"] == "NEUTRAL"
        assert gating["confidence"] == 0


class TestBuildCombinedReason:
    def test_agreement(self):
        msg = main._build_combined_reason(
            ai_result={"signal": "BUY"},
            technical_result={"signal": "BUY"},
            combined={"agreement": True, "signal": "BUY"},
            score_combined_signal="BUY",
            ai_score=0.8, technical_score=1.0, combined_score=0.88,
        )
        assert "Concordância" in msg
        assert "+0.88" in msg

    def test_partial_neutral(self):
        msg = main._build_combined_reason(
            ai_result={"signal": "BUY"},
            technical_result={"signal": "NEUTRAL"},
            combined={"agreement": False, "signal": "NEUTRAL"},
            score_combined_signal="BUY",
            ai_score=0.6, technical_score=0.0, combined_score=0.36,
        )
        assert "NEUTRAL" in msg
        assert "estrita" in msg

    def test_disagreement(self):
        msg = main._build_combined_reason(
            ai_result={"signal": "BUY"},
            technical_result={"signal": "SELL"},
            combined={"agreement": False, "signal": "NEUTRAL"},
            score_combined_signal="NEUTRAL",
            ai_score=0.6, technical_score=-1.0, combined_score=-0.04,
        )
        assert "Discordância" in msg
