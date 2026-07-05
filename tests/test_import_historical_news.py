"""Testes para `scripts.import_historical_news` — chunking, filtragem,
orçamento diário e retomabilidade."""

import json
from datetime import datetime, timezone

import pytest

from scripts import import_historical_news as ihn


def _article(title="ECB raises rates", summary="", time_published="20230115T120000", source="reuters"):
    return {
        "title": title,
        "summary": summary,
        "url": f"https://example.com/{title.replace(' ', '-')}-{time_published}",
        "source": source,
        "time_published": time_published,
    }


class TestMonthlyWindows:
    def test_splits_year_into_twelve_months(self):
        windows = ihn._monthly_windows(
            datetime(2023, 1, 1, tzinfo=timezone.utc), datetime(2024, 1, 1, tzinfo=timezone.utc),
        )
        assert len(windows) == 12
        assert windows[0][0] == datetime(2023, 1, 1, tzinfo=timezone.utc)
        assert windows[0][1] == datetime(2023, 2, 1, tzinfo=timezone.utc)
        assert windows[-1][0] == datetime(2023, 12, 1, tzinfo=timezone.utc)

    def test_partial_final_month_clamped_to_date_to(self):
        windows = ihn._monthly_windows(
            datetime(2023, 1, 1, tzinfo=timezone.utc), datetime(2023, 1, 15, tzinfo=timezone.utc),
        )
        assert len(windows) == 1
        assert windows[0] == (
            datetime(2023, 1, 1, tzinfo=timezone.utc), datetime(2023, 1, 15, tzinfo=timezone.utc),
        )


class TestParseAvTime:
    def test_converts_to_iso_utc(self):
        assert ihn._parse_av_time("20230106T215200") == "2023-01-06T21:52:00+00:00"


class TestIsRelevant:
    def test_matches_keyword_in_title(self):
        assert ihn._is_relevant(_article(title="Fed signals rate pause")) is True

    def test_matches_keyword_in_summary(self):
        assert ihn._is_relevant(_article(title="Market update", summary="The euro rallied today")) is True

    def test_no_match_returns_false(self):
        assert ihn._is_relevant(_article(title="Local sports team wins", summary="Great game")) is False


class TestLoadApiKeys:
    def test_no_key_configured_returns_empty(self, monkeypatch):
        monkeypatch.delenv("ALPHA_VANTAGE_KEY_HISTORICAL", raising=False)
        for i in range(2, 6):
            monkeypatch.delenv(f"ALPHA_VANTAGE_KEY_HISTORICAL_{i}", raising=False)
        assert ihn._load_api_keys() == []

    def test_single_key(self, monkeypatch):
        monkeypatch.setenv("ALPHA_VANTAGE_KEY_HISTORICAL", "key-1")
        monkeypatch.delenv("ALPHA_VANTAGE_KEY_HISTORICAL_2", raising=False)
        assert ihn._load_api_keys() == ["key-1"]

    def test_loads_five_sequential_keys(self, monkeypatch):
        monkeypatch.setenv("ALPHA_VANTAGE_KEY_HISTORICAL", "key-1")
        for i in range(2, 6):
            monkeypatch.setenv(f"ALPHA_VANTAGE_KEY_HISTORICAL_{i}", f"key-{i}")
        monkeypatch.delenv("ALPHA_VANTAGE_KEY_HISTORICAL_6", raising=False)

        assert ihn._load_api_keys() == ["key-1", "key-2", "key-3", "key-4", "key-5"]

    def test_stops_at_first_gap(self, monkeypatch):
        monkeypatch.setenv("ALPHA_VANTAGE_KEY_HISTORICAL", "key-1")
        monkeypatch.setenv("ALPHA_VANTAGE_KEY_HISTORICAL_2", "key-2")
        monkeypatch.delenv("ALPHA_VANTAGE_KEY_HISTORICAL_3", raising=False)
        monkeypatch.setenv("ALPHA_VANTAGE_KEY_HISTORICAL_4", "key-4")  # ignorada — há um buraco em _3

        assert ihn._load_api_keys() == ["key-1", "key-2"]

    def test_placeholder_treated_as_unset(self, monkeypatch):
        monkeypatch.setenv("ALPHA_VANTAGE_KEY_HISTORICAL", "PLACEHOLDER")
        assert ihn._load_api_keys() == []

    def test_never_reads_live_alpha_vantage_key(self, monkeypatch):
        monkeypatch.delenv("ALPHA_VANTAGE_KEY_HISTORICAL", raising=False)
        monkeypatch.setenv("ALPHA_VANTAGE_KEY", "live-key-used-by-daily-bot")
        assert ihn._load_api_keys() == []


class TestImportHistoricalNews:
    def test_respects_daily_budget_and_saves_state(self, memory_db, monkeypatch, tmp_path):
        from modules import database

        calls = []

        def fake_fetch(api_key, time_from, time_to, limit=1000):
            calls.append((time_from, time_to))
            return [_article(time_published=f"{time_from[:8]}T120000")]

        monkeypatch.setattr(ihn, "_fetch_window", fake_fetch)
        state_path = tmp_path / "state.json"

        stats = ihn.import_historical_news(
            "EUR/USD",
            datetime(2023, 1, 1, tzinfo=timezone.utc),
            datetime(2023, 4, 1, tzinfo=timezone.utc),
            api_keys="fake-key",
            state_path=state_path,
            daily_budget=2,
            sleep_seconds=0,
        )

        assert stats["requests_made"] == 2
        assert stats["months_total"] == 3
        assert stats["months_done"] == 2
        assert stats["months_remaining"] == 1
        assert len(calls) == 2

        rows = memory_db.execute("SELECT COUNT(*) AS n FROM news_items").fetchone()
        assert rows["n"] == 2

    def test_resumes_from_state_without_refetching_done_months(self, memory_db, monkeypatch, tmp_path):
        state_path = tmp_path / "state.json"
        state_path.write_text(json.dumps({"done": ["2023-01", "2023-02"]}))

        calls = []

        def fake_fetch(api_key, time_from, time_to, limit=1000):
            calls.append(time_from)
            return [_article(time_published=f"{time_from[:8]}T120000")]

        monkeypatch.setattr(ihn, "_fetch_window", fake_fetch)

        stats = ihn.import_historical_news(
            "EUR/USD",
            datetime(2023, 1, 1, tzinfo=timezone.utc),
            datetime(2023, 4, 1, tzinfo=timezone.utc),
            api_keys="fake-key",
            state_path=state_path,
            daily_budget=10,
            sleep_seconds=0,
        )

        assert len(calls) == 1  # só março, jan/fev já feitos
        assert stats["months_remaining"] == 0

    def test_alternates_between_multiple_keys_and_doubles_default_budget(self, memory_db, monkeypatch, tmp_path):
        keys_used = []

        def fake_fetch(api_key, time_from, time_to, limit=1000):
            keys_used.append(api_key)
            return [_article(time_published=f"{time_from[:8]}T120000")]

        monkeypatch.setattr(ihn, "_fetch_window", fake_fetch)
        state_path = tmp_path / "state.json"

        stats = ihn.import_historical_news(
            "EUR/USD",
            datetime(2023, 1, 1, tzinfo=timezone.utc),
            datetime(2023, 5, 1, tzinfo=timezone.utc),
            api_keys=["key-a", "key-b"],
            state_path=state_path,
            sleep_seconds=0,
        )

        assert stats["requests_made"] == 4  # 4 meses, dentro do orçamento 2x20
        assert keys_used == ["key-a", "key-b", "key-a", "key-b"]

    def test_filters_out_irrelevant_articles(self, memory_db, monkeypatch, tmp_path):
        def fake_fetch(api_key, time_from, time_to, limit=1000):
            return [
                _article(title="ECB decision", time_published=f"{time_from[:8]}T100000"),
                _article(title="Local bakery wins award", summary="tasty bread", time_published=f"{time_from[:8]}T110000"),
            ]

        monkeypatch.setattr(ihn, "_fetch_window", fake_fetch)
        state_path = tmp_path / "state.json"

        stats = ihn.import_historical_news(
            "EUR/USD",
            datetime(2023, 1, 1, tzinfo=timezone.utc),
            datetime(2023, 2, 1, tzinfo=timezone.utc),
            api_keys="fake-key",
            state_path=state_path,
            daily_budget=10,
            sleep_seconds=0,
        )
        assert stats["imported"] == 1

    def test_no_new_requests_when_all_months_done(self, memory_db, monkeypatch, tmp_path):
        state_path = tmp_path / "state.json"
        state_path.write_text(json.dumps({"done": ["2023-01"]}))

        def fake_fetch(*a, **k):
            raise AssertionError("não deveria chamar a API — mês já concluído")

        monkeypatch.setattr(ihn, "_fetch_window", fake_fetch)

        stats = ihn.import_historical_news(
            "EUR/USD",
            datetime(2023, 1, 1, tzinfo=timezone.utc),
            datetime(2023, 2, 1, tzinfo=timezone.utc),
            api_keys="fake-key",
            state_path=state_path,
            daily_budget=10,
            sleep_seconds=0,
        )
        assert stats["requests_made"] == 0
        assert stats["months_remaining"] == 0

    def test_rate_limit_rotates_to_next_key_and_retries_same_month(self, memory_db, monkeypatch, tmp_path):
        calls = []

        def fake_fetch(api_key, time_from, time_to, limit=1000):
            calls.append(api_key)
            if api_key == "key-a":
                raise ihn.RateLimitError("our standard API rate limit is 25 requests per day")
            return [_article(time_published=f"{time_from[:8]}T120000")]

        monkeypatch.setattr(ihn, "_fetch_window", fake_fetch)
        state_path = tmp_path / "state.json"

        stats = ihn.import_historical_news(
            "EUR/USD",
            datetime(2023, 1, 1, tzinfo=timezone.utc),
            datetime(2023, 2, 1, tzinfo=timezone.utc),
            api_keys=["key-a", "key-b"],
            state_path=state_path,
            sleep_seconds=0,
        )

        assert calls == ["key-a", "key-b"]  # key-a esgotada, roda para key-b
        assert stats["months_done"] == 1
        assert stats["keys_exhausted"] == 1

    def test_all_keys_exhausted_stops_gracefully_without_marking_month_done(self, memory_db, monkeypatch, tmp_path):
        def fake_fetch(api_key, time_from, time_to, limit=1000):
            raise ihn.RateLimitError("standard API rate limit is 25 requests per day")

        monkeypatch.setattr(ihn, "_fetch_window", fake_fetch)
        state_path = tmp_path / "state.json"

        stats = ihn.import_historical_news(
            "EUR/USD",
            datetime(2023, 1, 1, tzinfo=timezone.utc),
            datetime(2023, 2, 1, tzinfo=timezone.utc),
            api_keys=["key-a", "key-b"],
            state_path=state_path,
            sleep_seconds=0,
        )

        assert stats["months_done"] == 0
        assert stats["months_remaining"] == 1
        assert stats["keys_exhausted"] == 2

    def test_non_rate_limit_information_message_raises_runtime_error(self, memory_db, monkeypatch):
        class FakeResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {"Information": "Invalid API call, please check the parameters."}

        monkeypatch.setattr(ihn.requests, "get", lambda *a, **k: FakeResponse())

        with pytest.raises(RuntimeError):
            ihn._fetch_window("fake-key", "20230101T0000", "20230201T0000")

    def test_rate_limit_information_message_raises_rate_limit_error(self, memory_db, monkeypatch):
        class FakeResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {"Information": "our standard API rate limit is 25 requests per day"}

        monkeypatch.setattr(ihn.requests, "get", lambda *a, **k: FakeResponse())

        with pytest.raises(ihn.RateLimitError):
            ihn._fetch_window("fake-key", "20230101T0000", "20230201T0000")

    def test_db_path_override_writes_to_alternate_file(self, memory_db, monkeypatch, tmp_path):
        from modules import database

        def fake_fetch(api_key, time_from, time_to, limit=1000):
            return [_article(time_published=f"{time_from[:8]}T120000")]

        monkeypatch.setattr(ihn, "_fetch_window", fake_fetch)
        alt_db = tmp_path / "isolated.db"

        ihn.import_historical_news(
            "EUR/USD",
            datetime(2023, 1, 1, tzinfo=timezone.utc),
            datetime(2023, 2, 1, tzinfo=timezone.utc),
            api_keys="fake-key",
            state_path=tmp_path / "state.json",
            daily_budget=10,
            sleep_seconds=0,
            db_path=str(alt_db),
        )

        assert alt_db.exists()
        rows = memory_db.execute("SELECT COUNT(*) AS n FROM news_items").fetchone()
        assert rows["n"] == 0  # nada foi escrito na DB de produção/teste
