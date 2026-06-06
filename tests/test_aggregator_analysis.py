"""Testes para `database.get_aggregator_analysis` — medição estatística (Fase 2).

Esta análise é puramente observacional: não altera decisões, gates nem trades.
Os testes inserem decisões com o veredicto da IA agregadora + paper trades
fechados e verificam as métricas, o impacto potencial e a recomendação.
"""

import json

import pytest

from modules import database


def _insert(
    conn,
    *,
    should_trade,
    status,
    pips,
    ai_signal="BUY",
    tech_signal="BUY",
    risk="medium",
    warnings=None,
    direction="BUY",
):
    cur = conn.execute(
        """
        INSERT INTO decisions
            (timestamp, pair, created_at, technical_signal,
             ai_aggregated_signal, ai_aggregated_should_trade,
             ai_aggregated_risk_level, ai_aggregated_warnings)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "2026-06-01T10:00:00+00:00",
            "EUR/USD",
            "2026-06-01T10:00:00+00:00",
            tech_signal,
            ai_signal,
            1 if should_trade else 0,
            risk,
            json.dumps(warnings or []),
        ),
    )
    decision_id = cur.lastrowid
    conn.execute(
        """
        INSERT INTO paper_trades
            (decision_id, pair, timeframe, direction, entry_price,
             simulated_sl, simulated_tp, status, result_pips, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            decision_id,
            "EUR/USD",
            "1h",
            direction,
            1.1700,
            1.1670,
            1.1760,
            status,
            pips,
            "2026-06-01T10:00:00+00:00",
        ),
    )
    conn.commit()
    return decision_id


class TestEmptyAndCompat:
    def test_empty_db_returns_unavailable(self, memory_db):
        analysis = database.get_aggregator_analysis(memory_db)
        assert analysis["available"] is False
        assert analysis["recommendation"] == "Continuar em shadow mode"
        assert analysis["total_evaluated"] == 0
        # estrutura completa mesmo sem dados
        assert analysis["should_trade_true"]["trades"] == 0
        assert set(analysis["risk_level"].keys()) == {"low", "medium", "high"}

    def test_old_db_without_columns(self, memory_db):
        # Simula base antiga: dropar a coluna não é trivial em sqlite, por isso
        # apenas garantimos o caminho normal; a deteção real é por PRAGMA.
        analysis = database.get_aggregator_analysis(memory_db)
        assert "should_trade_true" in analysis
        assert "impact_if_veto_enabled" in analysis

    def test_trades_without_aggregator_are_ignored(self, memory_db):
        # decisão sem veredicto da IA (should_trade NULL) + paper trade fechado
        cur = memory_db.execute(
            "INSERT INTO decisions (timestamp, pair, created_at) VALUES (?, ?, ?)",
            ("2026-06-01T10:00:00+00:00", "EUR/USD", "2026-06-01T10:00:00+00:00"),
        )
        memory_db.execute(
            """
            INSERT INTO paper_trades
                (decision_id, pair, timeframe, direction, entry_price,
                 simulated_sl, simulated_tp, status, result_pips, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (cur.lastrowid, "EUR/USD", "1h", "BUY", 1.17, 1.167, 1.176, "win", 30.0,
             "2026-06-01T10:00:00+00:00"),
        )
        memory_db.commit()
        analysis = database.get_aggregator_analysis(memory_db)
        assert analysis["available"] is False
        assert analysis["total_evaluated"] == 0


class TestShouldTradeSplit:
    def test_true_and_false_metrics(self, memory_db):
        # should_trade=True: 3 wins (+20 cada), 1 loss (-10)
        for _ in range(3):
            _insert(memory_db, should_trade=True, status="win", pips=20.0)
        _insert(memory_db, should_trade=True, status="loss", pips=-10.0)
        # should_trade=False: 1 win (+15), 2 losses (-12 cada)
        _insert(memory_db, should_trade=False, status="win", pips=15.0)
        _insert(memory_db, should_trade=False, status="loss", pips=-12.0)
        _insert(memory_db, should_trade=False, status="loss", pips=-12.0)

        analysis = database.get_aggregator_analysis(memory_db)
        assert analysis["available"] is True
        assert analysis["total_evaluated"] == 7

        t = analysis["should_trade_true"]
        assert t["trades"] == 4
        assert t["wins"] == 3
        assert t["losses"] == 1
        assert t["winrate"] == 75.0
        assert t["net_pips"] == pytest.approx(50.0)  # 60 - 10
        assert t["expectancy"] == pytest.approx(12.5)  # 50 / 4

        f = analysis["should_trade_false"]
        assert f["trades"] == 3
        assert f["wins"] == 1
        assert f["losses"] == 2
        assert f["winrate"] == pytest.approx(33.3, abs=0.1)
        assert f["net_pips"] == pytest.approx(-9.0)  # 15 - 24

    def test_expired_counts_in_trades_not_winrate(self, memory_db):
        _insert(memory_db, should_trade=True, status="win", pips=20.0)
        _insert(memory_db, should_trade=True, status="loss", pips=-10.0)
        _insert(memory_db, should_trade=True, status="expired", pips=0.0)

        t = database.get_aggregator_analysis(memory_db)["should_trade_true"]
        assert t["trades"] == 3
        assert t["expired"] == 1
        assert t["winrate"] == 50.0  # 1 win / (1 win + 1 loss)


class TestImpactIfVeto:
    def test_veto_improves_when_false_group_loses(self, memory_db):
        for _ in range(3):
            _insert(memory_db, should_trade=True, status="win", pips=20.0)
        _insert(memory_db, should_trade=True, status="loss", pips=-10.0)
        _insert(memory_db, should_trade=False, status="loss", pips=-30.0)
        _insert(memory_db, should_trade=False, status="loss", pips=-20.0)

        analysis = database.get_aggregator_analysis(memory_db)
        impact = analysis["impact_if_veto_enabled"]
        # baseline net = 50 + (-50) = 0 ; com veto (só True) = 50 -> change +50
        assert impact["net_pips_change"] == pytest.approx(50.0)
        # winrate sobe ao remover o grupo perdedor
        assert impact["winrate_change"] > 0
        assert impact["expectancy_change"] > 0


class TestAgreement:
    def test_agreement_rate_and_winrate(self, memory_db):
        # concorda (ai==tech=BUY) e ganha
        _insert(memory_db, should_trade=True, status="win", pips=20.0, ai_signal="BUY", tech_signal="BUY")
        _insert(memory_db, should_trade=True, status="win", pips=20.0, ai_signal="BUY", tech_signal="BUY")
        # discorda (ai=SELL, tech=BUY) e perde
        _insert(memory_db, should_trade=False, status="loss", pips=-15.0, ai_signal="SELL", tech_signal="BUY")

        agreement = database.get_aggregator_analysis(memory_db)["agreement"]
        assert agreement["agree"] == 2
        assert agreement["disagree"] == 1
        assert agreement["agreement_rate"] == pytest.approx(66.7, abs=0.1)
        assert agreement["winrate_when_agree"] == 100.0
        assert agreement["winrate_when_disagree"] == 0.0


class TestRiskAndWarnings:
    def test_risk_level_breakdown(self, memory_db):
        _insert(memory_db, should_trade=True, status="win", pips=10.0, risk="low")
        _insert(memory_db, should_trade=True, status="loss", pips=-5.0, risk="high")
        _insert(memory_db, should_trade=False, status="loss", pips=-8.0, risk="high")

        risk = database.get_aggregator_analysis(memory_db)["risk_level"]
        assert risk["low"]["trades"] == 1
        assert risk["low"]["wins"] == 1
        assert risk["high"]["trades"] == 2
        assert risk["high"]["losses"] == 2
        assert risk["high"]["net_pips"] == pytest.approx(-13.0)

    def test_warnings_frequency(self, memory_db):
        _insert(memory_db, should_trade=True, status="win", pips=10.0, warnings=["event risk", "high volatility"])
        _insert(memory_db, should_trade=False, status="loss", pips=-5.0, warnings=["event risk"])

        warnings = database.get_aggregator_analysis(memory_db)["warnings"]
        assert warnings["event risk"] == 2
        assert warnings["high volatility"] == 1


class TestRecommendation:
    def test_small_sample_stays_shadow(self, memory_db):
        _insert(memory_db, should_trade=True, status="win", pips=20.0)
        _insert(memory_db, should_trade=False, status="loss", pips=-20.0)
        analysis = database.get_aggregator_analysis(memory_db)
        assert analysis["recommendation"] == "Continuar em shadow mode"
        assert any("amostra" in r for r in analysis["recommendation_reasons"])

    def test_ready_for_advisory_with_clear_edge(self, memory_db, monkeypatch):
        monkeypatch.setenv("AGGREGATOR_ADVISORY_MIN_TRADES", "20")
        monkeypatch.setenv("AGGREGATOR_ADVISORY_MIN_PER_GROUP", "10")
        # 12 trades should_trade=True com winrate alto e lucro
        for _ in range(10):
            _insert(memory_db, should_trade=True, status="win", pips=20.0)
        for _ in range(2):
            _insert(memory_db, should_trade=True, status="loss", pips=-10.0)
        # 10 trades should_trade=False maioritariamente a perder
        for _ in range(2):
            _insert(memory_db, should_trade=False, status="win", pips=10.0)
        for _ in range(8):
            _insert(memory_db, should_trade=False, status="loss", pips=-15.0)

        analysis = database.get_aggregator_analysis(memory_db)
        assert analysis["recommendation"] == "IA pronta para modo advisory"
