"""Testes para Rolling Market Context — memória contextual do mercado.

Cobre os 10 critérios de aceitação especificados:
1. Criação da tabela rolling_market_context.
2. Guardar contexto.
3. Ler último contexto.
4. Snapshot inclui latest_rolling_market_context.
5. Falha da IA não quebra o ciclo.
6. Export_logs inclui latest e recent.
7. calibration_report mostra secção sem quebrar em DB antiga.
8. Rolling context não altera evaluate_trade.
9. Rolling context não altera risk_engine.
10. Rolling context fica desativado se ROLLING_CONTEXT_ENABLED=False.
"""

import json
import os
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules import database, rolling_context, context_snapshot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SAMPLE_CONTEXT_DATA = {
    "market_phase": "trend",
    "macro_bias": "bullish_eur",
    "technical_bias": "BUY",
    "combined_bias": "BUY",
    "confidence": 72,
    "risk_level": "low",
    "short_summary": "EUR/USD em tendência ascendente com suporte técnico.",
    "what_changed": "RSI subiu acima de 60.",
    "persistent_factors": ["EMA20 > EMA50", "Macro positivo"],
    "new_factors": ["RSI bullish"],
    "invalidated_factors": [],
    "key_risks": ["CPI amanhã"],
    "likely_market_intent": "Continuação da tendência ascendente.",
    "recommended_stance": "trade_normally",
    "should_trade_bias": True,
    "should_reduce_risk": False,
    "warnings": [],
}

MINIMAL_SNAPSHOT = {
    "pair": "EUR/USD",
    "technical": {
        "current_price": 1.0952,
        "rsi": 58.0,
        "rsi_signal": "neutral",
        "ema_trend": "bullish",
        "macd_signal": "bullish",
        "atr_pips": 10.2,
        "adx": 22.0,
        "technical_signal": "BUY",
        "multi_timeframe_score": 0.35,
        "timeframe_alignment": "h1_h4_aligned",
    },
    "fundamental": {
        "ai_bias": "BUY",
        "ai_confidence": 65,
        "macro_context": "positive",
        "news_sentiment": "bullish",
        "volatility_context": "normal",
        "dangerous_event_nearby": False,
        "dangerous_event_reason": "",
    },
    "preliminary_recommendation": {
        "combined_signal": "BUY",
        "combined_confidence": 68,
        "combined_score": 0.42,
        "hold_off": False,
    },
    "performance": {"window_days": 7, "winrate": 55.0, "net_pips": 22.0, "loss_streak": 0},
}


# ---------------------------------------------------------------------------
# Teste 1 — Criação da tabela rolling_market_context
# ---------------------------------------------------------------------------

class TestTableCreation:
    def test_table_exists_after_init_db(self, memory_db):
        tables = {
            row[0]
            for row in memory_db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "rolling_market_context" in tables

    def test_table_has_expected_columns(self, memory_db):
        cols = {
            row["name"]
            for row in memory_db.execute(
                "PRAGMA table_info(rolling_market_context)"
            ).fetchall()
        }
        required = {
            "id", "created_at", "pair", "previous_context_id",
            "market_phase", "macro_bias", "technical_bias", "combined_bias",
            "confidence", "risk_level", "short_summary", "what_changed",
            "persistent_factors_json", "new_factors_json", "invalidated_factors_json",
            "key_risks_json", "likely_market_intent", "recommended_stance",
            "should_trade_bias", "should_reduce_risk", "raw_response_json",
        }
        assert required.issubset(cols)


# ---------------------------------------------------------------------------
# Teste 2 — Guardar contexto
# ---------------------------------------------------------------------------

class TestSaveRollingContext:
    def test_save_returns_id(self, memory_db):
        saved_id = database.save_rolling_market_context(
            memory_db, pair="EUR/USD", data=SAMPLE_CONTEXT_DATA
        )
        assert isinstance(saved_id, int)
        assert saved_id > 0

    def test_save_persists_all_fields(self, memory_db):
        database.save_rolling_market_context(
            memory_db, pair="EUR/USD", data=SAMPLE_CONTEXT_DATA,
            previous_context_id=None,
            raw_response={"raw": True},
        )
        row = memory_db.execute(
            "SELECT * FROM rolling_market_context ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row["pair"] == "EUR/USD"
        assert row["market_phase"] == "trend"
        assert row["macro_bias"] == "bullish_eur"
        assert row["combined_bias"] == "BUY"
        assert row["confidence"] == 72
        assert row["risk_level"] == "low"
        assert row["should_trade_bias"] == 1
        assert row["should_reduce_risk"] == 0
        risks = json.loads(row["key_risks_json"])
        assert "CPI amanhã" in risks

    def test_save_with_previous_context_id(self, memory_db):
        first_id = database.save_rolling_market_context(
            memory_db, pair="EUR/USD", data=SAMPLE_CONTEXT_DATA
        )
        second_id = database.save_rolling_market_context(
            memory_db, pair="EUR/USD", data=SAMPLE_CONTEXT_DATA,
            previous_context_id=first_id,
        )
        row = memory_db.execute(
            "SELECT previous_context_id FROM rolling_market_context WHERE id = ?",
            (second_id,),
        ).fetchone()
        assert row["previous_context_id"] == first_id


# ---------------------------------------------------------------------------
# Teste 3 — Ler último contexto
# ---------------------------------------------------------------------------

class TestGetLatestRollingContext:
    def test_returns_none_when_empty(self, memory_db):
        result = database.get_latest_rolling_market_context(memory_db, "EUR/USD")
        assert result is None

    def test_returns_latest_row(self, memory_db):
        database.save_rolling_market_context(
            memory_db, pair="EUR/USD",
            data={**SAMPLE_CONTEXT_DATA, "short_summary": "Primeiro"},
        )
        database.save_rolling_market_context(
            memory_db, pair="EUR/USD",
            data={**SAMPLE_CONTEXT_DATA, "short_summary": "Segundo"},
        )
        result = database.get_latest_rolling_market_context(memory_db, "EUR/USD")
        assert result is not None
        assert result["short_summary"] == "Segundo"

    def test_deserialises_json_lists(self, memory_db):
        database.save_rolling_market_context(
            memory_db, pair="EUR/USD", data=SAMPLE_CONTEXT_DATA
        )
        result = database.get_latest_rolling_market_context(memory_db, "EUR/USD")
        assert isinstance(result["persistent_factors"], list)
        assert isinstance(result["key_risks"], list)
        assert "CPI amanhã" in result["key_risks"]

    def test_recent_returns_ordered_asc(self, memory_db):
        for summary in ("A", "B", "C"):
            database.save_rolling_market_context(
                memory_db, pair="EUR/USD",
                data={**SAMPLE_CONTEXT_DATA, "short_summary": summary},
            )
        recent = database.get_recent_rolling_market_context(memory_db, "EUR/USD", limit=10)
        summaries = [r["short_summary"] for r in recent]
        assert summaries == ["A", "B", "C"]

    def test_recent_respects_limit(self, memory_db):
        for i in range(5):
            database.save_rolling_market_context(
                memory_db, pair="EUR/USD",
                data={**SAMPLE_CONTEXT_DATA, "short_summary": str(i)},
            )
        recent = database.get_recent_rolling_market_context(memory_db, "EUR/USD", limit=3)
        assert len(recent) == 3


# ---------------------------------------------------------------------------
# Teste 4 — Snapshot inclui latest_rolling_market_context
# ---------------------------------------------------------------------------

class TestSnapshotIncludesRollingContext:
    def _base_snapshot(self, latest_rolling=None):
        return context_snapshot.build_market_snapshot(
            "EUR/USD",
            technical_result={
                "signal": "BUY",
                "indicators": {"current_price": 1.095, "rsi": 58.0},
            },
            ai_result={"signal": "BUY", "bias": "BUY", "confidence": 60},
            combined={"signal": "BUY", "confidence": 60, "combined_score": 0.40},
            gating_combined={"signal": "BUY", "confidence": 60, "hold_off": False},
            trade_decision={"trade_allowed": True, "gate_diagnostics": {"config": {}}},
            gate_context={},
            event_risk={},
            performance={},
            latest_rolling_context=latest_rolling,
        )

    def test_snapshot_without_rolling_context_has_no_key(self):
        snap = self._base_snapshot(latest_rolling=None)
        assert "latest_rolling_market_context" not in snap

    def test_snapshot_with_rolling_context_has_key(self):
        snap = self._base_snapshot(latest_rolling=SAMPLE_CONTEXT_DATA)
        assert "latest_rolling_market_context" in snap

    def test_snapshot_rolling_context_fields(self):
        snap = self._base_snapshot(latest_rolling=SAMPLE_CONTEXT_DATA)
        ctx = snap["latest_rolling_market_context"]
        assert ctx["market_phase"] == "trend"
        assert ctx["combined_bias"] == "BUY"
        assert ctx["recommended_stance"] == "trade_normally"
        assert isinstance(ctx["key_risks"], list)


# ---------------------------------------------------------------------------
# Teste 5 — Falha da IA não quebra o ciclo
# ---------------------------------------------------------------------------

class TestAIFailureIsSafe:
    def test_update_returns_fallback_on_ai_error(self, memory_db, monkeypatch):
        monkeypatch.setenv("ROLLING_CONTEXT_ENABLED", "True")
        monkeypatch.setenv("AI_PROVIDER", "groq")

        def _boom(*args, **kwargs):
            raise RuntimeError("IA offline")

        monkeypatch.setattr(rolling_context, "_call_groq", _boom)
        monkeypatch.setattr(rolling_context, "_call_claude", _boom)

        result = rolling_context.update(
            memory_db, pair="EUR/USD", snapshot=MINIMAL_SNAPSHOT
        )
        assert result is not None
        assert result["risk_level"] == "medium"
        assert result["combined_bias"] == "NEUTRAL"
        assert "rolling_context_failed" in result["warnings"]

    def test_update_returns_fallback_on_bad_json(self, memory_db, monkeypatch):
        monkeypatch.setenv("AI_PROVIDER", "groq")
        monkeypatch.setattr(rolling_context, "_call_groq", lambda *a, **k: "não é json")

        result = rolling_context.update(
            memory_db, pair="EUR/USD", snapshot=MINIMAL_SNAPSHOT
        )
        assert result is not None
        assert result["combined_bias"] == "NEUTRAL"

    def test_update_keeps_previous_summary_in_fallback(self, memory_db, monkeypatch):
        monkeypatch.setenv("AI_PROVIDER", "groq")
        database.save_rolling_market_context(
            memory_db, pair="EUR/USD", data=SAMPLE_CONTEXT_DATA
        )
        monkeypatch.setattr(rolling_context, "_call_groq", lambda *a, **k: "bad json")

        result = rolling_context.update(
            memory_db, pair="EUR/USD", snapshot=MINIMAL_SNAPSHOT
        )
        assert "[fallback]" in result["short_summary"]


# ---------------------------------------------------------------------------
# Teste 6 — Export_logs inclui latest e recent
# ---------------------------------------------------------------------------

class TestExportLogsIncludesRollingContext:
    def test_read_rolling_market_context_structure(self, tmp_path, monkeypatch):
        from scripts import export_logs

        db_file = tmp_path / "test.db"
        monkeypatch.setattr(export_logs, "DB_PATH", db_file)
        monkeypatch.setattr(database, "DB_PATH", db_file)

        conn = sqlite3.connect(db_file)
        conn.row_factory = sqlite3.Row
        database.init_db(conn)
        database.save_rolling_market_context(conn, pair="EUR/USD", data=SAMPLE_CONTEXT_DATA)
        conn.close()

        result = export_logs._read_rolling_market_context("EUR/USD", recent_limit=5)
        assert "latest" in result
        assert "recent" in result
        assert result["latest"] is not None
        assert result["latest"]["market_phase"] == "trend"
        assert len(result["recent"]) == 1

    def test_read_rolling_context_returns_empty_when_no_db(self, tmp_path, monkeypatch):
        from scripts import export_logs
        monkeypatch.setattr(export_logs, "DB_PATH", tmp_path / "nonexistent.db")
        result = export_logs._read_rolling_market_context("EUR/USD")
        assert result == {"latest": None, "recent": []}


# ---------------------------------------------------------------------------
# Teste 7 — calibration_report mostra secção sem quebrar em DB antiga
# ---------------------------------------------------------------------------

class TestCalibrationReportRollingSection:
    def test_prints_no_context_when_empty(self, memory_db, capsys):
        from scripts.calibration_report import _print_rolling_context_section
        _print_rolling_context_section(memory_db)
        captured = capsys.readouterr()
        assert "ROLLING MARKET CONTEXT" in captured.out
        assert "sem contexto guardado ainda" in captured.out

    def test_prints_context_fields_when_present(self, memory_db, capsys):
        from scripts.calibration_report import _print_rolling_context_section
        database.save_rolling_market_context(
            memory_db, pair="EUR/USD", data=SAMPLE_CONTEXT_DATA
        )
        _print_rolling_context_section(memory_db)
        captured = capsys.readouterr()
        assert "market_phase" in captured.out
        assert "trend" in captured.out
        assert "trade_normally" in captured.out
        assert "CPI amanhã" in captured.out

    def test_does_not_raise_on_old_db_missing_table(self, tmp_path, monkeypatch, capsys):
        from scripts.calibration_report import _print_rolling_context_section
        db_file = tmp_path / "old.db"
        old_conn = sqlite3.connect(db_file)
        old_conn.row_factory = sqlite3.Row
        # DB antiga sem a tabela rolling_market_context
        old_conn.execute(
            "CREATE TABLE decisions (id INTEGER PRIMARY KEY, pair TEXT)"
        )
        old_conn.commit()

        # Deve imprimir a secção sem levantar excepção
        _print_rolling_context_section(old_conn)
        captured = capsys.readouterr()
        assert "ROLLING MARKET CONTEXT" in captured.out
        old_conn.close()


# ---------------------------------------------------------------------------
# Teste 8 — Rolling context não altera evaluate_trade
# ---------------------------------------------------------------------------

class TestRollingContextDoesNotAffectTrade:
    def test_evaluate_trade_unaffected(self, memory_db, monkeypatch):
        from modules.risk import evaluate_trade

        monkeypatch.setenv("DRY_RUN", "True")
        monkeypatch.setenv("ACCOUNT_BALANCE", "1000")
        monkeypatch.setenv("RISK_PER_TRADE_PERCENT", "1")
        monkeypatch.setenv("DEFAULT_STOP_LOSS_PIPS", "30")
        monkeypatch.setenv("DEFAULT_TAKE_PROFIT_PIPS", "60")

        gating = {"signal": "BUY", "confidence": 70, "hold_off": False}
        event_risk = {"dangerous_event_nearby": False}

        result_before = evaluate_trade(
            "EUR/USD", gating, 1.0950, event_risk, atr_pips=12.0,
            technical_indicators={}, gate_context={},
        )

        # Salvar contexto rolling não deve alterar avaliação
        database.save_rolling_market_context(
            memory_db, pair="EUR/USD", data=SAMPLE_CONTEXT_DATA
        )

        result_after = evaluate_trade(
            "EUR/USD", gating, 1.0950, event_risk, atr_pips=12.0,
            technical_indicators={}, gate_context={},
        )

        assert result_before["trade_allowed"] == result_after["trade_allowed"]
        assert result_before.get("block_reason") == result_after.get("block_reason")


# ---------------------------------------------------------------------------
# Teste 9 — Rolling context não altera risk_engine
# ---------------------------------------------------------------------------

class TestRollingContextDoesNotAffectRiskEngine:
    def test_risk_engine_unaffected_by_rolling_context(self, memory_db, monkeypatch):
        from modules.risk_engine import AdaptiveRiskEngine

        monkeypatch.setenv("ADAPTIVE_BASE_MIN_CONFIDENCE", "0.45")
        monkeypatch.setenv("ADAPTIVE_MIN_FLOOR", "0.35")
        monkeypatch.setenv("ADAPTIVE_MIN_CEILING", "0.65")

        engine = AdaptiveRiskEngine()
        gate_context = {"combined_score": 0.5, "technical_score": 0.4}

        combined_signal_dict = {"signal": "BUY", "confidence": 70, "combined_score": 0.5}

        result_before = engine.evaluate(
            signal="BUY", confidence=0.70, combined_signal=combined_signal_dict,
            event_risk={"dangerous_event_nearby": False},
            atr_pips=12.0, technical_indicators={}, gate_context=gate_context,
        )

        database.save_rolling_market_context(
            memory_db, pair="EUR/USD", data=SAMPLE_CONTEXT_DATA
        )

        result_after = engine.evaluate(
            signal="BUY", confidence=0.70, combined_signal=combined_signal_dict,
            event_risk={"dangerous_event_nearby": False},
            atr_pips=12.0, technical_indicators={}, gate_context=gate_context,
        )

        assert result_before["allow_trade"] == result_after["allow_trade"]


# ---------------------------------------------------------------------------
# Teste 10 — Rolling context desativado se ROLLING_CONTEXT_ENABLED=False
# ---------------------------------------------------------------------------

class TestRollingContextDisabled:
    def test_update_every_cycle_false_skips_update(self, memory_db, monkeypatch):
        monkeypatch.setenv("ROLLING_CONTEXT_ENABLED", "True")
        monkeypatch.setenv("ROLLING_CONTEXT_UPDATE_EVERY_CYCLE", "False")

        called = []

        def _fake_groq(*args, **kwargs):
            called.append(True)
            return json.dumps({
                **SAMPLE_CONTEXT_DATA,
                "persistent_factors": [],
                "new_factors": [],
                "invalidated_factors": [],
                "key_risks": [],
                "warnings": [],
            })

        monkeypatch.setattr(rolling_context, "_call_groq", _fake_groq)

        # Simula _run_rolling_context com ROLLING_CONTEXT_UPDATE_EVERY_CYCLE=False
        # A função update() em rolling_context não verifica a flag; é main.py que faz.
        # Aqui testamos o comportamento de main._run_rolling_context.
        import main as main_module
        monkeypatch.setattr(main_module, "_env_bool", lambda name, default: {
            "ROLLING_CONTEXT_ENABLED": True,
            "ROLLING_CONTEXT_UPDATE_EVERY_CYCLE": False,
        }.get(name, default))
        # `_should_update_rolling_context` usa `datetime.now(timezone.utc)`
        # internamente (não aceita `now_utc` injectado) e a sua regra nº1 é
        # "sem contexto anterior -> actualizar sempre" — como `memory_db` é
        # sempre uma DB nova, essa regra disparava incondicionalmente e o
        # "skip" nunca era exercitado, independentemente da hora real. O que
        # este teste quer verificar é apenas o *gating* em `_run_rolling_context`
        # (chama `_should_update_rolling_context` e respeita o resultado) — não
        # a lógica de "é a altura certa" dessa função, que não é o alvo aqui.
        # Fixamos o resultado directamente para tornar o teste determinístico.
        monkeypatch.setattr(main_module, "_should_update_rolling_context", lambda conn: False)

        result = main_module._run_rolling_context(
            memory_db, {}, {}, {}, {}, {}, {}, {}, {}, "score", None
        )
        assert result is None
        assert len(called) == 0

    def test_disabled_entirely_returns_none(self, memory_db, monkeypatch):
        monkeypatch.setenv("ROLLING_CONTEXT_ENABLED", "False")

        called = []

        def _fake_groq(*args, **kwargs):
            called.append(True)
            return json.dumps(SAMPLE_CONTEXT_DATA)

        monkeypatch.setattr(rolling_context, "_call_groq", _fake_groq)

        import main as main_module
        monkeypatch.setattr(main_module, "_env_bool", lambda name, default: {
            "ROLLING_CONTEXT_ENABLED": False,
            "ROLLING_CONTEXT_UPDATE_EVERY_CYCLE": True,
        }.get(name, default))

        result = main_module._run_rolling_context(
            memory_db, {}, {}, {}, {}, {}, {}, {}, {}, "score", None
        )
        assert result is None
        assert len(called) == 0


# ---------------------------------------------------------------------------
# Normalise unit tests (bonus)
# ---------------------------------------------------------------------------

class TestNormalise:
    def test_normalise_valid_input(self):
        result = rolling_context.normalise(SAMPLE_CONTEXT_DATA)
        assert result["market_phase"] == "trend"
        assert result["combined_bias"] == "BUY"
        assert result["should_trade_bias"] is True
        assert result["should_reduce_risk"] is False

    def test_normalise_invalid_enums_use_fallback(self):
        raw = {**SAMPLE_CONTEXT_DATA, "market_phase": "invalid", "risk_level": "extreme"}
        result = rolling_context.normalise(raw)
        assert result["market_phase"] == "uncertain"
        assert result["risk_level"] == "medium"

    def test_normalise_none_returns_fallback(self):
        result = rolling_context.normalise(None)
        assert result["combined_bias"] == "NEUTRAL"
        assert "rolling_context_failed" in result["warnings"]

    def test_normalise_clamps_confidence(self):
        raw = {**SAMPLE_CONTEXT_DATA, "confidence": 150}
        result = rolling_context.normalise(raw)
        assert result["confidence"] == 100
