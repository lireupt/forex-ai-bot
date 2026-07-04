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
            api_key="fake-key",
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
            api_key="fake-key",
            state_path=state_path,
            daily_budget=10,
            sleep_seconds=0,
        )

        assert len(calls) == 1  # só março, jan/fev já feitos
        assert stats["months_remaining"] == 0

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
            api_key="fake-key",
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
            api_key="fake-key",
            state_path=state_path,
            daily_budget=10,
            sleep_seconds=0,
        )
        assert stats["requests_made"] == 0
        assert stats["months_remaining"] == 0
