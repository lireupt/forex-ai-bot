"""Testes para `scripts.build_historical_ai_cache` — cache diário,
orçamento de tokens e retomabilidade."""

import json
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


class TestBuildHistoricalAiCache:
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
