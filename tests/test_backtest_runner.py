"""Teste de fumo para `backtest_runner` — corre ponta-a-ponta sobre candles
sintéticas e confirma isolamento de dados (nunca escreve em paper_trades/
decisions) e que produz um run completo com decisões registadas."""

from datetime import datetime, timedelta, timezone

import pytest

import backtest_runner as br
from modules import database, decision_engine


def _synthetic_hourly_candles(n, start):
    candles = []
    price = 1.1000
    for i in range(n):
        # passeio aleatório determinístico (sem numpy/random) para ter
        # alguma variação sem depender de seed externa.
        drift = 0.00005 * ((i % 7) - 3)
        price = round(price + drift, 5)
        high = round(price + 0.0006, 5)
        low = round(price - 0.0006, 5)
        candles.append({
            "candle_time": (start + timedelta(hours=i)).isoformat(),
            "open": price, "high": high, "low": low, "close": price,
            "volume": 100.0,
        })
    return candles


class TestRunBacktestSmoke:
    def test_runs_end_to_end_and_isolates_production_tables(self, memory_db, monkeypatch, tmp_path):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        candles = _synthetic_hourly_candles(150, start)
        database.save_market_candles(memory_db, candles, "EUR/USD", "1h", "import")
        memory_db.commit()

        # backtest_runner abre a sua própria connection via database.connect();
        # a fixture memory_db já monkeypatchou database.DB_PATH para o mesmo
        # ficheiro temporário, por isso não é preciso passar --db aqui.
        date_from = (start + timedelta(hours=100)).isoformat()
        date_to = (start + timedelta(hours=120)).isoformat()

        stats = br.run_backtest("EUR/USD", date_from, date_to, config={"apply_spread": True})

        assert stats["total_candles"] == 21  # 100..120 inclusive, 1 candle/hora
        assert stats["total_decisions"] == stats["total_candles"]

        conn = database.connect()
        run_row = database.get_backtest_run(conn, stats["run_id"])
        assert run_row["status"] == "completed"
        assert run_row["pair"] == "EUR/USD"

        decisions = database.get_backtest_decisions(conn, stats["run_id"])
        assert len(decisions) == stats["total_decisions"]

        # Isolamento: nada foi escrito nas tabelas de produção.
        prod_trades = conn.execute("SELECT COUNT(*) AS n FROM paper_trades").fetchone()
        prod_decisions = conn.execute("SELECT COUNT(*) AS n FROM decisions").fetchone()
        assert prod_trades["n"] == 0
        assert prod_decisions["n"] == 0
        conn.close()

    def test_raises_when_no_candles_available(self, memory_db):
        with pytest.raises(ValueError):
            br.run_backtest(
                "EUR/USD",
                "2024-01-01T00:00:00+00:00",
                "2024-01-02T00:00:00+00:00",
            )


class TestPointInTimeStrictness:
    """Regressão: candles_up_to(tf, before_dt) incluía a candle EM
    before_dt, que só fecha no fim do seu próprio período — fuga de
    futuro descoberta ao comparar com trades reais de produção (Passo 7):
    o backtest usava o close da hora seguinte como "preço actual"."""

    def test_candle_at_before_dt_is_excluded(self, memory_db):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        candles = _synthetic_hourly_candles(5, start)
        database.save_market_candles(memory_db, candles, "EUR/USD", "1h", "import")
        memory_db.commit()

        provider = br.HistoricalProvider(memory_db, "EUR/USD")
        target = start + timedelta(hours=3)
        df = provider.candles_up_to("1h", target)

        assert len(df) == 3  # candles das horas 0, 1, 2 — não a da hora 3
        assert df.index[-1] < target

    def test_candle_exactly_at_before_dt_not_double_counted_across_iterations(self, memory_db):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        candles = _synthetic_hourly_candles(5, start)
        database.save_market_candles(memory_db, candles, "EUR/USD", "1h", "import")
        memory_db.commit()

        provider = br.HistoricalProvider(memory_db, "EUR/USD")
        df_at_2 = provider.candles_up_to("1h", start + timedelta(hours=2))
        df_at_3 = provider.candles_up_to("1h", start + timedelta(hours=3))
        # a candle da hora 2 só aparece pela primeira vez quando before_dt
        # avança para depois dela (hora 3), nunca em before_dt=hora 2.
        assert df_at_2.index[-1] == start + timedelta(hours=1)
        assert df_at_3.index[-1] == start + timedelta(hours=2)


class TestEnvFallback:
    """Regressão: backtest_runner nunca chamava load_dotenv() e sl_mult/
    tp_mult/expiry_bars/gating_mode nunca liam PAPER_TRADE_SL_MULT/
    TP_MULT/EXPIRY_BARS/GATING_MODE do ambiente (só main.py o fazia) —
    descoberto ao comparar contra trades reais de produção (Passo 7): o
    backtest usava sempre o default do PairSpec/código, ignorando o que
    estava realmente configurado no live."""

    def test_picks_up_paper_trade_mults_and_gating_mode_from_env(self, memory_db, monkeypatch):
        monkeypatch.setenv("PAPER_TRADE_SL_MULT", "2.0")
        monkeypatch.setenv("PAPER_TRADE_TP_MULT", "4.0")
        monkeypatch.setenv("PAPER_TRADE_EXPIRY_BARS", "12")
        monkeypatch.setenv("GATING_MODE", "strict")

        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        candles = _synthetic_hourly_candles(30, start)
        database.save_market_candles(memory_db, candles, "EUR/USD", "1h", "import")
        memory_db.commit()

        stats = br.run_backtest(
            "EUR/USD",
            (start + timedelta(hours=10)).isoformat(),
            (start + timedelta(hours=15)).isoformat(),
        )
        conn = database.connect()
        run_row = database.get_backtest_run(conn, stats["run_id"])
        conn.close()

        assert run_row["config"]["sl_mult"] == 2.0
        assert run_row["config"]["tp_mult"] == 4.0
        assert run_row["config"]["expiry_bars"] == 12
        assert run_row["config"]["gating_mode"] == "strict"

    def test_explicit_config_overrides_env(self, memory_db, monkeypatch):
        monkeypatch.setenv("PAPER_TRADE_SL_MULT", "2.0")

        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        candles = _synthetic_hourly_candles(30, start)
        database.save_market_candles(memory_db, candles, "EUR/USD", "1h", "import")
        memory_db.commit()

        stats = br.run_backtest(
            "EUR/USD",
            (start + timedelta(hours=10)).isoformat(),
            (start + timedelta(hours=15)).isoformat(),
            config={"sl_mult": 3.0},
        )
        conn = database.connect()
        run_row = database.get_backtest_run(conn, stats["run_id"])
        conn.close()
        assert run_row["config"]["sl_mult"] == 3.0


class TestMacroShapedEvents:
    """Regressão: get_macro_risk()/ai_analyst esperam eventos no formato
    ff_calendar (date/time/currency/event/impact). database.get_high_impact_
    events() devolve title/country/event_time (formato de tabela) — sem
    conversão, get_macro_risk() detecta ausência de 'date' e substitui os
    eventos point-in-time pelo calendário real de "esta semana" via scrape
    ao vivo, uma fuga de futuro silenciosa."""

    def test_shapes_db_row_into_ff_calendar_format(self):
        rows = [{
            "title": "Core CPI", "country": "USD", "impact": "high",
            "event_time": "2023-06-15T12:30:00+00:00", "source": "test",
        }]
        shaped = br._to_macro_shaped_events(rows)
        assert shaped == [{
            "date": "2023-06-15",
            "time": "2023-06-15T12:30:00+00:00",
            "currency": "USD",
            "impact": "high",
            "event": "Core CPI",
        }]

    def test_get_macro_risk_never_falls_back_to_live_calendar(self, memory_db, monkeypatch):
        from modules import macro_filter, database

        def _boom(*args, **kwargs):
            raise AssertionError("get_macro_risk não devia recorrer ao scrape ao vivo num backtest")

        monkeypatch.setattr("modules.ff_calendar.fetch_this_week", _boom)

        memory_db.execute(
            "INSERT INTO economic_events (title, country, impact, event_time, source, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("Core CPI", "USD", "high", "2023-06-15T12:30:00+00:00", "test", "2023-06-15T00:00:00+00:00"),
        )
        memory_db.commit()

        provider = br.HistoricalProvider(memory_db, "EUR/USD")
        result = macro_filter.get_macro_risk(
            "EUR/USD", datetime(2023, 6, 15, 12, 0, tzinfo=timezone.utc),
            events=provider.macro_events(),
        )
        assert result["macro_risk_level"] in ("none", "medium", "high")

    def test_high_impact_events_keeps_raw_shape_for_gate_check(self, memory_db):
        memory_db.execute(
            "INSERT INTO economic_events (title, country, impact, event_time, source, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("Core CPI", "USD", "high", "2023-06-15T12:30:00+00:00", "test", "2023-06-15T00:00:00+00:00"),
        )
        memory_db.commit()
        provider = br.HistoricalProvider(memory_db, "EUR/USD")
        raw = provider.high_impact_events()
        assert raw[0]["title"] == "Core CPI"
        assert raw[0]["event_time"] == "2023-06-15T12:30:00+00:00"


class TestRowsToDf:
    """Regressão: uma candle 1d gravada por outra parte do sistema (provider
    'yahoo') com candle_time sem offset de fuso partia pd.to_datetime()
    quando misturada com linhas com offset — descoberto ao correr o
    backtest sobre dados reais."""

    def test_handles_mixed_naive_and_aware_timestamps(self):
        rows = [
            {"candle_time": "2025-05-20T00:00:00+00:00", "open": 1.1, "high": 1.11, "low": 1.09, "close": 1.1, "volume": 0},
            {"candle_time": "2025-05-21T00:00:00", "open": 1.1, "high": 1.11, "low": 1.09, "close": 1.1, "volume": 0},
        ]
        df = br._rows_to_df(rows)
        assert len(df) == 2
        assert str(df.index.tz) == "UTC"


class TestHistoricalProviderCandleFilter:
    def test_candle_provider_filters_out_other_sources(self, memory_db):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        clean = _synthetic_hourly_candles(10, start)
        database.save_market_candles(memory_db, clean, "EUR/USD", "1h", "import")
        dirty = [{
            "candle_time": (start + timedelta(hours=5)).isoformat(),
            "open": 9.9, "high": 9.9, "low": 9.9, "close": 9.9, "volume": 0,
        }]
        database.save_market_candles(memory_db, dirty, "EUR/USD", "1h", "yahoo")
        memory_db.commit()

        provider_filtered = br.HistoricalProvider(memory_db, "EUR/USD", candle_provider="import")
        rows = provider_filtered.driving_candles("1h", start.isoformat(), (start + timedelta(hours=20)).isoformat())
        assert len(rows) == 10  # só as "import", não a "yahoo" extra

        provider_unfiltered = br.HistoricalProvider(memory_db, "EUR/USD")
        rows_all = provider_unfiltered.driving_candles("1h", start.isoformat(), (start + timedelta(hours=20)).isoformat())
        assert len(rows_all) == 11  # sem filtro, vê as 10 "import" + 1 "yahoo" extra


def _cached_ai_result(**overrides):
    base = {
        "signal": "BUY", "confidence": 80, "reasoning": "teste", "risk_level": "medium",
        "hold_off": False, "provider": "groq", "bias": "BUY",
        "confidence_adjustment": 0.4, "risk_adjustment": 0.0,
    }
    base.update(overrides)
    return base


class TestBuildHistoricalAiResultProvider:
    def test_returns_cached_result_for_day_and_none_for_uncached_day(self, memory_db):
        database.save_ai_analysis(memory_db, "EUR/USD", "2023-01-05", "h1", _cached_ai_result())
        memory_db.commit()

        lookup = br.build_historical_ai_result_provider(
            memory_db, "EUR/USD",
            datetime(2023, 1, 1, tzinfo=timezone.utc), datetime(2023, 1, 10, tzinfo=timezone.utc),
            provider="groq",
        )

        result = lookup("2023-01-05T14:30:00+00:00")
        assert result is not None
        assert result["signal"] == "BUY"
        assert result["confidence_adjustment"] == 0.4

        assert lookup("2023-01-06T14:30:00+00:00") is None  # sem cache nesse dia

    def test_accepts_string_date_bounds(self, memory_db):
        database.save_ai_analysis(memory_db, "EUR/USD", "2023-01-05", "h1", _cached_ai_result(signal="SELL"))
        memory_db.commit()

        lookup = br.build_historical_ai_result_provider(
            memory_db, "EUR/USD", "2023-01-01T00:00:00+00:00", "2023-01-10T00:00:00+00:00",
        )
        assert lookup("2023-01-05T08:00:00+00:00")["signal"] == "SELL"


class TestRunBacktestWithHistoricalAi:
    def test_injects_cached_ai_result_per_day_and_falls_back_to_default_when_missing(
        self, memory_db, monkeypatch,
    ):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        candles = _synthetic_hourly_candles(150, start)
        database.save_market_candles(memory_db, candles, "EUR/USD", "1h", "import")

        # hour 100 -> 2024-01-05T04:00, hour 130 -> 2024-01-06T10:00: a
        # janela do backtest atravessa a fronteira do dia.
        database.save_ai_analysis(memory_db, "EUR/USD", "2024-01-05", "h1", _cached_ai_result())
        memory_db.commit()

        seen_ai_results = {}
        real_decide = decision_engine.decide

        def spy_decide(ctx):
            seen_ai_results.setdefault(ctx.now.date().isoformat(), ctx.ai_result)
            return real_decide(ctx)

        monkeypatch.setattr(br.decision_engine, "decide", spy_decide)

        provider = br.build_historical_ai_result_provider(
            memory_db, "EUR/USD",
            start + timedelta(hours=100), start + timedelta(hours=130),
            provider="groq",
        )

        date_from = (start + timedelta(hours=100)).isoformat()
        date_to = (start + timedelta(hours=130)).isoformat()
        br.run_backtest("EUR/USD", date_from, date_to, ai_result_provider=provider)

        assert seen_ai_results["2024-01-05"]["signal"] == "BUY"
        assert seen_ai_results["2024-01-05"]["confidence_adjustment"] == 0.4
        # sem cache para este dia, ctx.ai_result fica None -> decide() usa
        # DEFAULT_AI_RESULT internamente (fallback da Fase A).
        assert seen_ai_results["2024-01-06"] is None

    def test_without_provider_stays_fase_a_default(self, memory_db, monkeypatch):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        candles = _synthetic_hourly_candles(150, start)
        database.save_market_candles(memory_db, candles, "EUR/USD", "1h", "import")
        memory_db.commit()

        seen = []
        real_decide = decision_engine.decide

        def spy_decide(ctx):
            seen.append(ctx.ai_result)
            return real_decide(ctx)

        monkeypatch.setattr(br.decision_engine, "decide", spy_decide)

        date_from = (start + timedelta(hours=100)).isoformat()
        date_to = (start + timedelta(hours=105)).isoformat()
        br.run_backtest("EUR/USD", date_from, date_to)

        assert all(r is None for r in seen)  # sem provider -> decide() usa DEFAULT_AI_RESULT
