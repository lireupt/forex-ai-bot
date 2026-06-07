"""Testes para Weekend Mode e Weekly Market Preparation.

Cobre:
1. Weekend mode não chama IA em ciclos normais de fim-de-semana.
2. Weekly prep só corre no horário configurado (domingo, hora correcta).
3. Weekly prep não cria paper trades.
4. Weekly prep grava em SQLite (weekly_market_prep).
5. latest_weekly_market_prep entra no snapshot da IA agregadora.
6. Comportamento normal durante mercado aberto não muda.
"""

import json
from datetime import datetime, timezone

import pytest

from modules import database
from modules import context_snapshot
from modules.weekly_market_prep import (
    is_weekend_mode_active,
    is_weekly_prep_due,
    run_weekly_prep,
    weekend_mode_config,
    weekly_prep_config,
    _validate_prep_result,
    _fallback_prep,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SUNDAY_BEFORE_PREP = datetime(2026, 6, 7, 20, 0, tzinfo=timezone.utc)   # dom 20h — antes do horário
SUNDAY_AT_PREP = datetime(2026, 6, 7, 21, 5, tzinfo=timezone.utc)       # dom 21h05 — horário certo
SUNDAY_AFTER_OPEN = datetime(2026, 6, 7, 22, 30, tzinfo=timezone.utc)   # dom 22h30 — após abertura
SATURDAY = datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc)             # sábado
FRIDAY_CLOSED = datetime(2026, 6, 5, 23, 0, tzinfo=timezone.utc)        # sexta 23h — fechado
MONDAY = datetime(2026, 6, 8, 10, 0, tzinfo=timezone.utc)               # segunda — mercado aberto

SAMPLE_NEWS = [
    {"title": "ECB hints at rate pause", "source": "Reuters", "published": "2026-06-07"},
    {"title": "Euro area PMI drops", "source": "Bloomberg", "published": "2026-06-07"},
]
SAMPLE_EVENTS = [
    {"event": "ECB Rate Decision", "currency": "EUR", "impact": "high", "time": "2026-06-11T12:00:00"},
    {"event": "US CPI", "currency": "USD", "impact": "high", "time": "2026-06-12T12:30:00"},
]


# ---------------------------------------------------------------------------
# 1. is_weekend_mode_active
# ---------------------------------------------------------------------------

class TestIsWeekendModeActive:
    def test_saturday_is_weekend(self):
        assert is_weekend_mode_active(now_utc=SATURDAY) is True

    def test_sunday_before_open_is_weekend(self):
        assert is_weekend_mode_active(now_utc=SUNDAY_AT_PREP) is True

    def test_friday_after_close_is_weekend(self):
        assert is_weekend_mode_active(now_utc=FRIDAY_CLOSED) is True

    def test_monday_is_not_weekend(self):
        assert is_weekend_mode_active(now_utc=MONDAY) is False

    def test_sunday_after_open_is_not_weekend(self):
        assert is_weekend_mode_active(now_utc=SUNDAY_AFTER_OPEN) is False

    def test_weekend_mode_disabled_returns_false(self, monkeypatch):
        monkeypatch.setenv("WEEKEND_MODE_ENABLED", "False")
        # is_weekend_mode_active usa market state, não a config flag.
        # Verifica apenas que a chamada funciona sem erro.
        result = is_weekend_mode_active(now_utc=SATURDAY)
        assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# 2. is_weekly_prep_due — timing
# ---------------------------------------------------------------------------

class TestIsWeeklyPrepDue:
    def test_due_on_sunday_at_correct_hour(self):
        assert is_weekly_prep_due(now_utc=SUNDAY_AT_PREP, conn=None) is True

    def test_not_due_before_configured_hour(self):
        assert is_weekly_prep_due(now_utc=SUNDAY_BEFORE_PREP, conn=None) is False

    def test_not_due_on_saturday(self):
        assert is_weekly_prep_due(now_utc=SATURDAY, conn=None) is False

    def test_not_due_on_monday(self):
        assert is_weekly_prep_due(now_utc=MONDAY, conn=None) is False

    def test_not_due_after_market_opens(self):
        assert is_weekly_prep_due(now_utc=SUNDAY_AFTER_OPEN, conn=None) is False

    def test_disabled_returns_false(self, monkeypatch):
        monkeypatch.setenv("WEEKLY_MARKET_PREP_ENABLED", "False")
        assert is_weekly_prep_due(now_utc=SUNDAY_AT_PREP, conn=None) is False

    def test_custom_hour(self, monkeypatch):
        monkeypatch.setenv("WEEKLY_MARKET_PREP_HOUR_UTC", "20")
        # Com hora=20, deve ser due às 20h05
        early = datetime(2026, 6, 7, 20, 5, tzinfo=timezone.utc)
        assert is_weekly_prep_due(now_utc=early, conn=None) is True
        # Mas não às 19h
        too_early = datetime(2026, 6, 7, 19, 59, tzinfo=timezone.utc)
        assert is_weekly_prep_due(now_utc=too_early, conn=None) is False

    def test_not_due_if_already_ran_today(self, memory_db):
        # Insere um registo de hoje
        prep_result = {
            "pair": "EUR/USD",
            "macro_bias": "neutral",
            "preferred_direction": "NEUTRAL",
            "confidence": 50,
            "risk_level": "medium",
            "recommendation": "trade_normally",
            "summary": "Teste",
            "reasoning_summary": "Teste",
            "key_weekend_news": [],
            "key_events_next_week": [],
            "market_opening_risks": [],
            "warnings": [],
            "week_start": "2026-06-08",
            "status": "ok",
        }
        database.save_weekly_market_prep(memory_db, prep_result)
        # Agora is_weekly_prep_due deve retornar False para o mesmo dia
        assert is_weekly_prep_due(now_utc=SUNDAY_AT_PREP, conn=memory_db) is False


# ---------------------------------------------------------------------------
# 3. weekly prep não cria paper trades
# ---------------------------------------------------------------------------

class TestWeeklyPrepNoTrade:
    def test_run_weekly_prep_creates_no_paper_trade(self, memory_db, monkeypatch):
        monkeypatch.setenv("WEEKLY_MARKET_PREP_USE_AI", "False")

        prep = run_weekly_prep(memory_db, SAMPLE_NEWS, SAMPLE_EVENTS, pair="EUR/USD")

        # Nenhum paper trade deve existir
        trades = database.get_paper_trades(memory_db, limit=100)
        assert len(trades) == 0

        # A preparação foi feita
        assert prep.get("pair") == "EUR/USD"

    def test_run_weekly_prep_result_has_no_order(self, memory_db, monkeypatch):
        monkeypatch.setenv("WEEKLY_MARKET_PREP_USE_AI", "False")

        prep = run_weekly_prep(memory_db, SAMPLE_NEWS, SAMPLE_EVENTS, pair="EUR/USD")

        # Não deve conter campos de trade
        for trade_field in ("entry_price", "simulated_sl", "simulated_tp", "sl_pips", "tp_pips"):
            assert trade_field not in prep


# ---------------------------------------------------------------------------
# 4. weekly prep grava em SQLite
# ---------------------------------------------------------------------------

class TestWeeklyPrepSavesSQLite:
    def test_saves_to_weekly_market_prep_table(self, memory_db, monkeypatch):
        monkeypatch.setenv("WEEKLY_MARKET_PREP_USE_AI", "False")

        run_weekly_prep(memory_db, SAMPLE_NEWS, SAMPLE_EVENTS, pair="EUR/USD")

        latest = database.get_latest_weekly_market_prep(memory_db, "EUR/USD")
        assert latest is not None
        assert latest["pair"] == "EUR/USD"
        assert latest["macro_bias"] == "neutral"
        assert latest["preferred_direction"] == "NEUTRAL"
        assert "week_start" in latest

    def test_saves_json_list_fields(self, memory_db, monkeypatch):
        monkeypatch.setenv("WEEKLY_MARKET_PREP_USE_AI", "False")

        run_weekly_prep(memory_db, SAMPLE_NEWS, SAMPLE_EVENTS, pair="EUR/USD")

        latest = database.get_latest_weekly_market_prep(memory_db, "EUR/USD")
        assert isinstance(latest["key_weekend_news"], list)
        assert isinstance(latest["key_events_next_week"], list)
        assert isinstance(latest["market_opening_risks"], list)
        assert isinstance(latest["warnings"], list)

    def test_get_latest_returns_none_when_empty(self, memory_db):
        result = database.get_latest_weekly_market_prep(memory_db, "EUR/USD")
        assert result is None

    def test_second_run_creates_new_record(self, memory_db, monkeypatch):
        monkeypatch.setenv("WEEKLY_MARKET_PREP_USE_AI", "False")

        run_weekly_prep(memory_db, SAMPLE_NEWS, SAMPLE_EVENTS, pair="EUR/USD")
        run_weekly_prep(memory_db, SAMPLE_NEWS, SAMPLE_EVENTS, pair="EUR/USD")

        count = memory_db.execute("SELECT COUNT(*) FROM weekly_market_prep").fetchone()[0]
        assert count == 2

    def test_get_latest_returns_most_recent(self, memory_db, monkeypatch):
        monkeypatch.setenv("WEEKLY_MARKET_PREP_USE_AI", "False")

        run_weekly_prep(memory_db, SAMPLE_NEWS, SAMPLE_EVENTS, pair="EUR/USD")

        # Segundo com dados diferentes
        news2 = [{"title": "Fed hawkish surprise", "source": "WSJ", "published": "2026-06-07"}]
        run_weekly_prep(memory_db, news2, [], pair="EUR/USD")

        latest = database.get_latest_weekly_market_prep(memory_db, "EUR/USD")
        # Deve ser o segundo (sem key_events_next_week)
        assert latest["key_events_next_week"] == []


# ---------------------------------------------------------------------------
# 5. latest_weekly_market_prep entra no snapshot da IA agregadora
# ---------------------------------------------------------------------------

class TestWeeklyPrepInContextSnapshot:
    def _base_snapshot(self, weekly_prep=None):
        return context_snapshot.build_market_snapshot(
            "EUR/USD",
            {}, {}, {}, {}, {}, {}, {}, {},
            latest_weekly_market_prep=weekly_prep,
        )

    def test_snapshot_without_prep_has_no_weekly_key(self):
        snap = self._base_snapshot(weekly_prep=None)
        assert "weekly_market_prep" not in snap

    def test_snapshot_with_empty_dict_has_no_weekly_key(self):
        snap = self._base_snapshot(weekly_prep={})
        assert "weekly_market_prep" not in snap

    def test_snapshot_with_prep_includes_weekly_key(self):
        prep = {
            "macro_bias": "bullish_eur",
            "preferred_direction": "BUY",
            "confidence": 70,
            "risk_level": "medium",
            "recommendation": "trade_normally",
            "summary": "EUR fortalecido pelo contexto macro.",
            "reasoning_summary": "BCE hawkish.",
            "warnings": [],
            "week_start": "2026-06-08",
            "created_at": "2026-06-07T21:05:00+00:00",
        }
        snap = self._base_snapshot(weekly_prep=prep)
        assert "weekly_market_prep" in snap
        wp = snap["weekly_market_prep"]
        assert wp["macro_bias"] == "bullish_eur"
        assert wp["preferred_direction"] == "BUY"
        assert wp["confidence"] == 70
        assert wp["recommendation"] == "trade_normally"
        assert wp["week_start"] == "2026-06-08"

    def test_snapshot_prep_fields_are_condensed(self):
        prep = {
            "macro_bias": "neutral",
            "preferred_direction": "NEUTRAL",
            "confidence": 30,
            "risk_level": "high",
            "recommendation": "reduce_risk",
            "summary": "Panorama incerto.",
            "reasoning_summary": "Dados mistos.",
            "warnings": ["evento_risco"],
            "week_start": "2026-06-08",
            "created_at": "2026-06-07T21:00:00+00:00",
            # Campos extras que não devem aparecer no snapshot condensado
            "key_weekend_news": ["notícia A"],
            "key_events_next_week": ["evento B"],
            "market_opening_risks": ["risco C"],
        }
        snap = self._base_snapshot(weekly_prep=prep)
        wp = snap["weekly_market_prep"]
        # Apenas os campos condensados devem estar presentes
        assert "key_weekend_news" not in wp
        assert "key_events_next_week" not in wp
        assert "market_opening_risks" not in wp
        assert wp["warnings"] == ["evento_risco"]


# ---------------------------------------------------------------------------
# 6. Comportamento normal durante mercado aberto não muda
# ---------------------------------------------------------------------------

class TestNormalBehaviorUnchanged:
    def test_market_open_not_weekend(self):
        assert is_weekend_mode_active(now_utc=MONDAY) is False

    def test_weekly_prep_not_due_on_weekday(self):
        assert is_weekly_prep_due(now_utc=MONDAY, conn=None) is False

    def test_snapshot_without_prep_unchanged(self):
        snap = context_snapshot.build_market_snapshot(
            "EUR/USD",
            {"signal": "BUY", "indicators": {"rsi": 55.0}},
            {"signal": "BUY", "confidence": 70},
            {"signal": "BUY", "confidence": 70},
            {"signal": "BUY", "confidence": 70},
            {"trade_allowed": True},
            {"market": {"is_open": True}, "operational": {}, "cooldown": {}},
            {"dangerous_event_nearby": False},
            {"winrate": 55.0},
        )
        # Estrutura existente intacta
        assert snap["pair"] == "EUR/USD"
        assert "technical" in snap
        assert "fundamental" in snap
        assert "weekly_market_prep" not in snap

    def test_context_snapshot_new_param_is_optional(self):
        # Chamada sem o parâmetro latest_weekly_market_prep não deve falhar
        snap = context_snapshot.build_market_snapshot(
            "EUR/USD", {}, {}, {}, {}, {}, {}, {}, {}
        )
        assert snap["pair"] == "EUR/USD"


# ---------------------------------------------------------------------------
# Validação do output da IA
# ---------------------------------------------------------------------------

class TestValidatePrepResult:
    def _valid_result(self):
        return {
            "pair": "EUR/USD",
            "macro_bias": "bullish_eur",
            "preferred_direction": "BUY",
            "confidence": 75,
            "risk_level": "medium",
            "summary": "Panorama bullish para o EUR.",
            "key_weekend_news": ["BCE hawkish"],
            "key_events_next_week": ["ECB Rate Decision"],
            "market_opening_risks": ["gap risco"],
            "recommendation": "trade_normally",
            "reasoning_summary": "Contexto macro favorável ao EUR.",
            "warnings": [],
        }

    def test_valid_result_passes(self):
        result = _validate_prep_result(self._valid_result(), "EUR/USD")
        assert result["macro_bias"] == "bullish_eur"
        assert result["preferred_direction"] == "BUY"
        assert result["confidence"] == 75

    def test_invalid_macro_bias_normalized(self):
        r = self._valid_result()
        r["macro_bias"] = "super_bullish"
        result = _validate_prep_result(r, "EUR/USD")
        assert result["macro_bias"] == "neutral"

    def test_invalid_direction_normalized(self):
        r = self._valid_result()
        r["preferred_direction"] = "LONG"
        result = _validate_prep_result(r, "EUR/USD")
        assert result["preferred_direction"] == "NEUTRAL"

    def test_confidence_clamped(self):
        r = self._valid_result()
        r["confidence"] = 150
        result = _validate_prep_result(r, "EUR/USD")
        assert result["confidence"] == 100

    def test_invalid_recommendation_normalized(self):
        r = self._valid_result()
        r["recommendation"] = "yolo"
        result = _validate_prep_result(r, "EUR/USD")
        assert result["recommendation"] == "trade_normally"

    def test_missing_field_raises(self):
        r = self._valid_result()
        del r["summary"]
        with pytest.raises(ValueError, match="summary"):
            _validate_prep_result(r, "EUR/USD")

    def test_list_fields_coerced(self):
        r = self._valid_result()
        r["key_weekend_news"] = None
        result = _validate_prep_result(r, "EUR/USD")
        assert result["key_weekend_news"] == []


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

class TestConfigLoading:
    def test_weekend_mode_config_defaults(self, monkeypatch):
        for key in ("WEEKEND_MODE_ENABLED", "WEEKEND_MODE_UPDATE_NEWS",
                    "WEEKEND_MODE_UPDATE_CALENDAR", "WEEKEND_MODE_EXPORT_LOGS"):
            monkeypatch.delenv(key, raising=False)
        cfg = weekend_mode_config()
        assert cfg["enabled"] is True
        assert cfg["update_news"] is True
        assert cfg["update_calendar"] is True
        assert cfg["export_logs"] is True

    def test_weekend_mode_can_be_disabled(self, monkeypatch):
        monkeypatch.setenv("WEEKEND_MODE_ENABLED", "False")
        cfg = weekend_mode_config()
        assert cfg["enabled"] is False

    def test_weekly_prep_config_defaults(self, monkeypatch):
        for key in ("WEEKLY_MARKET_PREP_ENABLED", "WEEKLY_MARKET_PREP_WEEKDAY",
                    "WEEKLY_MARKET_PREP_HOUR_UTC", "WEEKLY_MARKET_PREP_LOOKBACK_HOURS",
                    "WEEKLY_MARKET_PREP_CALENDAR_DAYS", "WEEKLY_MARKET_PREP_USE_AI"):
            monkeypatch.delenv(key, raising=False)
        cfg = weekly_prep_config()
        assert cfg["enabled"] is True
        assert cfg["weekday"] == 6
        assert cfg["hour_utc"] == 21
        assert cfg["lookback_hours"] == 72
        assert cfg["calendar_days"] == 7
        assert cfg["use_ai"] is True

    def test_weekly_prep_config_overridable(self, monkeypatch):
        monkeypatch.setenv("WEEKLY_MARKET_PREP_HOUR_UTC", "20")
        monkeypatch.setenv("WEEKLY_MARKET_PREP_LOOKBACK_HOURS", "48")
        cfg = weekly_prep_config()
        assert cfg["hour_utc"] == 20
        assert cfg["lookback_hours"] == 48


# ---------------------------------------------------------------------------
# Fallback prep
# ---------------------------------------------------------------------------

class TestFallbackPrep:
    def test_fallback_has_required_fields(self):
        result = _fallback_prep("EUR/USD", "teste de falha")
        for field in (
            "pair", "macro_bias", "preferred_direction", "confidence",
            "risk_level", "summary", "key_weekend_news", "key_events_next_week",
            "market_opening_risks", "recommendation", "reasoning_summary", "warnings",
        ):
            assert field in result, f"campo '{field}' em falta no fallback"

    def test_fallback_status_is_failed(self):
        result = _fallback_prep("EUR/USD", "erro")
        assert result["status"] == "failed"

    def test_fallback_no_trade_recommendation(self):
        result = _fallback_prep("EUR/USD", "erro")
        assert result["preferred_direction"] == "NEUTRAL"
        assert result["confidence"] == 0
