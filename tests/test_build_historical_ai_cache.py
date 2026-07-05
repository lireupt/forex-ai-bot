"""Testes para `scripts.build_historical_ai_cache` — cache diário,
orçamento de tokens e retomabilidade."""

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from modules import database
from scripts import build_historical_ai_cache as bhac


def _seed_candles(conn, pair="EUR/USD", days=10):
    start = datetime(2023, 1, 1, tzinfo=timezone.utc)
    for tf in ("15m", "1h", "4h", "1d"):
        candles = []
        step_hours = {"15m": 0.25, "1h": 1, "4h": 4, "1d": 24}[tf]
        n = int(days * 24 / step_hours)
        price = 1.10
        for i in range(n):
            price = round(price + 0.00001 * ((i % 5) - 2), 5)
            ts = start + timedelta(hours=i * step_hours)
            candles.append({
                "candle_time": ts.isoformat(),
                "open": price, "high": price + 0.0005, "low": price - 0.0005,
                "close": price, "volume": 10.0,
            })
        database.save_market_candles(conn, candles, pair, tf, "import")
    conn.commit()


FAKE_AI_RESULT = {
    "signal": "NEUTRAL", "confidence": 20, "reasoning": "teste",
    "risk_level": "medium", "hold_off": False, "bias": "NEUTRAL",
    "confidence_adjustment": 0.0, "risk_adjustment": 0.0,
    "macro_context": "", "volatility_context": "medium", "news_sentiment": "neutral",
    "reason": "teste", "provider": "groq", "model_version": "groq:test", "status": "ok",
}


class TestNewsForDay:
    def test_filters_by_lookback_window_point_in_time(self, memory_db):
        day = datetime(2023, 1, 10, tzinfo=timezone.utc)
        database.save_news_items(memory_db, [
            {"title": "old news", "summary": "", "url": "u1", "source": "s",
             "published": (day - timedelta(hours=100)).isoformat()},
            {"title": "recent news", "summary": "", "url": "u2", "source": "s",
             "published": (day - timedelta(hours=10)).isoformat()},
            {"title": "future news", "summary": "", "url": "u3", "source": "s",
             "published": (day + timedelta(hours=5)).isoformat()},
        ], "EUR/USD")
        memory_db.commit()

        news = bhac._news_for_day(memory_db, "EUR/USD", day, lookback_hours=72)
        titles = [n["title"] for n in news]
        assert titles == ["recent news"]


class TestBuildDayAnalysis:
    def test_calls_ai_once_and_reuses_cache_on_second_call(self, memory_db, monkeypatch):
        _seed_candles(memory_db)
        calls = []

        def fake_analyse(news, events, pair, technical=None, macro_context_snapshot=None):
            calls.append(1)
            return dict(FAKE_AI_RESULT)

        monkeypatch.setattr(bhac, "analyse_ai", fake_analyse)

        day = datetime(2023, 1, 5, tzinfo=timezone.utc)
        result1, hash1, cached1 = bhac.build_day_analysis(memory_db, "groq", "EUR/USD", day, "import")
        result2, hash2, cached2 = bhac.build_day_analysis(memory_db, "groq", "EUR/USD", day, "import")

        assert cached1 is False
        assert cached2 is True
        assert hash1 == hash2
        assert len(calls) == 1

    def test_failed_result_not_cached(self, memory_db, monkeypatch):
        _seed_candles(memory_db)

        def fake_analyse(news, events, pair, technical=None, macro_context_snapshot=None):
            return {**FAKE_AI_RESULT, "status": "failed"}

        monkeypatch.setattr(bhac, "analyse_ai", fake_analyse)

        day = datetime(2023, 1, 5, tzinfo=timezone.utc)
        _result1, _hash1, cached1 = bhac.build_day_analysis(memory_db, "groq", "EUR/USD", day, "import")
        _result2, _hash2, cached2 = bhac.build_day_analysis(memory_db, "groq", "EUR/USD", day, "import")

        assert cached1 is False
        assert cached2 is False  # falhou, não ficou em cache -> tenta de novo


class TestLoadGroqKeys:
    def test_no_key_configured_returns_empty(self, monkeypatch):
        monkeypatch.delenv("GROQ_API_KEY_HISTORICAL", raising=False)
        monkeypatch.delenv("GROQ_API_KEY_HISTORICAL_2", raising=False)
        assert bhac._load_groq_keys() == []

    def test_loads_sequential_keys_and_stops_at_gap(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY_HISTORICAL", "gkey-1")
        monkeypatch.setenv("GROQ_API_KEY_HISTORICAL_2", "gkey-2")
        monkeypatch.delenv("GROQ_API_KEY_HISTORICAL_3", raising=False)
        monkeypatch.setenv("GROQ_API_KEY_HISTORICAL_4", "gkey-4")  # ignorada — buraco em _3

        assert bhac._load_groq_keys() == ["gkey-1", "gkey-2"]


class TestBuildHistoricalAiCache:
    def test_rate_limit_rotates_to_next_groq_key_and_retries_same_day(self, memory_db, monkeypatch, tmp_path):
        _seed_candles(memory_db, days=5)
        monkeypatch.setenv("GROQ_API_KEY_HISTORICAL", "gkey-1")
        monkeypatch.setenv("GROQ_API_KEY_HISTORICAL_2", "gkey-2")
        monkeypatch.delenv("GROQ_API_KEY_HISTORICAL_3", raising=False)

        keys_seen = []

        def fake_analyse(news, events, pair, technical=None, macro_context_snapshot=None):
            keys_seen.append(os.environ.get("GROQ_API_KEY"))
            if os.environ.get("GROQ_API_KEY") == "gkey-1":
                return {**FAKE_AI_RESULT, "status": "failed", "error": "Rate limit reached for model, please retry"}
            return dict(FAKE_AI_RESULT)

        monkeypatch.setattr(bhac, "analyse_ai", fake_analyse)
        state_path = tmp_path / "state.json"

        stats = bhac.build_historical_ai_cache(
            "EUR/USD",
            datetime(2023, 1, 1, tzinfo=timezone.utc),
            datetime(2023, 1, 2, tzinfo=timezone.utc),
            candle_provider="import",
            state_path=state_path,
        )

        assert keys_seen == ["gkey-1", "gkey-2"]  # gkey-1 esgotada, roda para gkey-2
        assert stats["calls_made"] == 1
        assert stats["failed"] == 0
        assert stats["keys_exhausted"] == 1
        assert stats["days_processed"] == 1

    def test_all_groq_keys_exhausted_stops_gracefully_without_advancing_state(self, memory_db, monkeypatch, tmp_path):
        _seed_candles(memory_db, days=5)
        monkeypatch.setenv("GROQ_API_KEY_HISTORICAL", "gkey-1")
        monkeypatch.setenv("GROQ_API_KEY_HISTORICAL_2", "gkey-2")
        monkeypatch.delenv("GROQ_API_KEY_HISTORICAL_3", raising=False)

        def fake_analyse(news, events, pair, technical=None, macro_context_snapshot=None):
            return {**FAKE_AI_RESULT, "status": "failed", "error": "rate limit exceeded, quota used up"}

        monkeypatch.setattr(bhac, "analyse_ai", fake_analyse)
        state_path = tmp_path / "state.json"

        stats = bhac.build_historical_ai_cache(
            "EUR/USD",
            datetime(2023, 1, 1, tzinfo=timezone.utc),
            datetime(2023, 1, 2, tzinfo=timezone.utc),
            candle_provider="import",
            state_path=state_path,
        )

        assert stats["days_processed"] == 0
        assert stats["keys_exhausted"] == 2
        assert not state_path.exists() or json.loads(state_path.read_text()).get("last_day") is None

    def test_non_rate_limit_failure_does_not_rotate_keys(self, memory_db, monkeypatch, tmp_path):
        _seed_candles(memory_db, days=5)
        monkeypatch.setenv("GROQ_API_KEY_HISTORICAL", "gkey-1")
        monkeypatch.setenv("GROQ_API_KEY_HISTORICAL_2", "gkey-2")
        monkeypatch.delenv("GROQ_API_KEY_HISTORICAL_3", raising=False)

        calls = []

        def fake_analyse(news, events, pair, technical=None, macro_context_snapshot=None):
            calls.append(os.environ.get("GROQ_API_KEY"))
            return {**FAKE_AI_RESULT, "status": "failed", "error": "resposta não é JSON válido"}

        monkeypatch.setattr(bhac, "analyse_ai", fake_analyse)
        state_path = tmp_path / "state.json"

        stats = bhac.build_historical_ai_cache(
            "EUR/USD",
            datetime(2023, 1, 1, tzinfo=timezone.utc),
            datetime(2023, 1, 2, tzinfo=timezone.utc),
            candle_provider="import",
            state_path=state_path,
        )

        assert calls == ["gkey-1"]  # não rodou — falha não é de quota
        assert stats["failed"] == 1
        assert stats["keys_exhausted"] == 0
    def test_respects_token_budget(self, memory_db, monkeypatch, tmp_path):
        _seed_candles(memory_db, days=10)

        def fake_analyse(news, events, pair, technical=None, macro_context_snapshot=None):
            return dict(FAKE_AI_RESULT)

        monkeypatch.setattr(bhac, "analyse_ai", fake_analyse)
        state_path = tmp_path / "state.json"

        stats = bhac.build_historical_ai_cache(
            "EUR/USD",
            datetime(2023, 1, 1, tzinfo=timezone.utc),
            datetime(2023, 1, 10, tzinfo=timezone.utc),
            candle_provider="import",
            token_budget=3 * bhac.ESTIMATED_TOKENS_PER_CALL,
            state_path=state_path,
        )
        assert stats["calls_made"] == 3
        assert stats["days_processed"] == 3
        assert stats["days_remaining"] == 6

    def test_failed_day_does_not_advance_state_and_stops_loop(self, memory_db, monkeypatch, tmp_path):
        _seed_candles(memory_db, days=10)
        calls = []

        def fake_analyse(news, events, pair, technical=None, macro_context_snapshot=None):
            calls.append(1)
            if len(calls) == 2:
                return {**FAKE_AI_RESULT, "status": "failed"}
            return dict(FAKE_AI_RESULT)

        monkeypatch.setattr(bhac, "analyse_ai", fake_analyse)
        state_path = tmp_path / "state.json"

        stats = bhac.build_historical_ai_cache(
            "EUR/USD",
            datetime(2023, 1, 1, tzinfo=timezone.utc),
            datetime(2023, 1, 10, tzinfo=timezone.utc),
            candle_provider="import",
            token_budget=100 * bhac.ESTIMATED_TOKENS_PER_CALL,
            state_path=state_path,
        )

        # dia 1 (01-01) ok, dia 2 (01-02) falha -> pára sem avançar o estado
        assert stats["calls_made"] == 1
        assert stats["failed"] == 1
        state = json.loads(state_path.read_text())
        assert state["last_day"] == "2023-01-01T00:00:00+00:00"

        # a próxima corrida tenta o dia 01-02 de novo (não foi saltado)
        stats2 = bhac.build_historical_ai_cache(
            "EUR/USD",
            datetime(2023, 1, 1, tzinfo=timezone.utc),
            datetime(2023, 1, 10, tzinfo=timezone.utc),
            candle_provider="import",
            token_budget=100 * bhac.ESTIMATED_TOKENS_PER_CALL,
            state_path=state_path,
        )
        assert stats2["failed"] == 0  # desta vez a chamada teve sucesso (calls[2] não é a 2ª global)

    def test_resumes_from_state_file(self, memory_db, monkeypatch, tmp_path):
        _seed_candles(memory_db, days=10)

        def fake_analyse(news, events, pair, technical=None, macro_context_snapshot=None):
            return dict(FAKE_AI_RESULT)

        monkeypatch.setattr(bhac, "analyse_ai", fake_analyse)
        state_path = tmp_path / "state.json"
        state_path.write_text(json.dumps({"last_day": "2023-01-03T00:00:00+00:00"}))

        stats = bhac.build_historical_ai_cache(
            "EUR/USD",
            datetime(2023, 1, 1, tzinfo=timezone.utc),
            datetime(2023, 1, 10, tzinfo=timezone.utc),
            candle_provider="import",
            token_budget=100 * bhac.ESTIMATED_TOKENS_PER_CALL,
            state_path=state_path,
        )
        # Retoma a partir de 2023-01-04 (dia seguinte ao last_day) até 01-09 -> 6 dias
        assert stats["days_processed"] == 6
        assert stats["days_remaining"] == 0
