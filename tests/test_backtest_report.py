"""Testes para `scripts.backtest_report` — métricas e breakdowns."""

import pytest

from modules import database
from scripts import backtest_report as br


def _seed_run(conn, run_id="run1"):
    database.create_backtest_run(
        conn, run_id, "EUR/USD", "2024-01-01T00:00:00+00:00", "2024-03-01T00:00:00+00:00", {},
    )
    return run_id


def _trade(**overrides):
    base = {
        "direction": "BUY",
        "entry_price": 1.1000,
        "simulated_sl": 1.0980,
        "simulated_tp": 1.1040,
        "sl_pips": 20.0,
        "tp_pips": 40.0,
        "atr_pips": 20.0,
        "created_at": "2024-01-05T09:00:00+00:00",
        "expiry_at": "2024-01-05T15:00:00+00:00",
    }
    base.update(overrides)
    return base


class TestBuildReport:
    def test_unknown_run_id_raises(self, memory_db):
        with pytest.raises(ValueError):
            br.build_report(memory_db, "does-not-exist")

    def test_winrate_profit_factor_and_pips(self, memory_db):
        run_id = _seed_run(memory_db)
        for status, pips, r in [("win", 40.0, 2.0), ("win", 40.0, 2.0), ("loss", -20.0, -1.0)]:
            trade_id = database.save_backtest_trade(memory_db, run_id, "EUR/USD", _trade())
            memory_db.execute(
                "UPDATE backtest_trades SET status=?, result_pips=?, result_r_multiple=?, "
                "close_price=1.1, closed_at='2024-01-05T12:00:00+00:00' WHERE id=?",
                (status, pips, r, trade_id),
            )
        memory_db.commit()

        report = br.build_report(memory_db, run_id)
        assert report["closed_trades"] == 3
        assert report["winrate"] == pytest.approx(66.7, abs=0.1)
        assert report["total_pips"] == 60.0
        # profit_factor = gross_profit(4.0) / gross_loss(1.0)
        assert report["profit_factor"] == pytest.approx(4.0)

    def test_longest_losing_streak_spans_whole_history_not_just_tail(self, memory_db):
        run_id = _seed_run(memory_db)
        # loss, loss, win, loss -> maior sequência é 2 (no início), não 1 (no fim)
        for status in ("loss", "loss", "win", "loss"):
            trade_id = database.save_backtest_trade(memory_db, run_id, "EUR/USD", _trade())
            memory_db.execute(
                "UPDATE backtest_trades SET status=?, result_pips=-10, result_r_multiple=-1, "
                "closed_at='2024-01-05T12:00:00+00:00' WHERE id=?",
                (status, trade_id),
            )
        memory_db.commit()
        report = br.build_report(memory_db, run_id)
        assert report["longest_losing_streak"] == 2

    def test_monthly_breakdown_groups_by_month(self, memory_db):
        run_id = _seed_run(memory_db)
        jan_id = database.save_backtest_trade(memory_db, run_id, "EUR/USD", _trade(created_at="2024-01-10T09:00:00+00:00"))
        feb_id = database.save_backtest_trade(memory_db, run_id, "EUR/USD", _trade(created_at="2024-02-10T09:00:00+00:00"))
        memory_db.execute(
            "UPDATE backtest_trades SET status='win', result_pips=40, result_r_multiple=2, "
            "closed_at='2024-01-10T12:00:00+00:00' WHERE id=?", (jan_id,),
        )
        memory_db.execute(
            "UPDATE backtest_trades SET status='loss', result_pips=-20, result_r_multiple=-1, "
            "closed_at='2024-02-10T12:00:00+00:00' WHERE id=?", (feb_id,),
        )
        memory_db.commit()
        report = br.build_report(memory_db, run_id)
        months = {row["month"]: row for row in report["monthly"]}
        assert months["2024-01"]["trades"] == 1
        assert months["2024-01"]["winrate"] == 100.0
        assert months["2024-02"]["trades"] == 1
        assert months["2024-02"]["winrate"] == 0.0

    def test_session_breakdown_buckets_by_utc_hour(self, memory_db):
        run_id = _seed_run(memory_db)
        # 09:00 UTC -> London session
        trade_id = database.save_backtest_trade(memory_db, run_id, "EUR/USD", _trade(created_at="2024-01-10T09:00:00+00:00"))
        memory_db.execute(
            "UPDATE backtest_trades SET status='win', result_pips=40, result_r_multiple=2, "
            "closed_at='2024-01-10T12:00:00+00:00' WHERE id=?", (trade_id,),
        )
        memory_db.commit()
        report = br.build_report(memory_db, run_id)
        assert len(report["sessions"]) == 1
        assert report["sessions"][0]["session"] == "London"

    def test_blocking_reason_distribution(self, memory_db):
        run_id = _seed_run(memory_db)
        database.save_backtest_decision(memory_db, run_id, "2024-01-05T09:00:00+00:00", {
            "signal": "NEUTRAL", "confidence": 0, "combined_score": 0.0,
            "trade_allowed": False, "block_reason": None,
            "blocking_reason": "sinal combinado é NEUTRAL",
        })
        database.save_backtest_decision(memory_db, run_id, "2024-01-05T10:00:00+00:00", {
            "signal": "BUY", "confidence": 80, "combined_score": 0.5,
            "trade_allowed": True, "block_reason": None, "blocking_reason": "",
        })
        memory_db.commit()
        report = br.build_report(memory_db, run_id)
        reasons = {row["reason"]: row for row in report["blocking_reasons"]}
        assert reasons["sinal combinado é NEUTRAL"]["count"] == 1
        assert reasons["(none)"]["count"] == 1

    def test_open_and_expired_trades_excluded_from_closed_metrics(self, memory_db):
        run_id = _seed_run(memory_db)
        database.save_backtest_trade(memory_db, run_id, "EUR/USD", _trade())  # fica "open"
        expired_id = database.save_backtest_trade(memory_db, run_id, "EUR/USD", _trade())
        memory_db.execute(
            "UPDATE backtest_trades SET status='expired', result_pips=5, result_r_multiple=0.25, "
            "closed_at='2024-01-05T15:00:00+00:00' WHERE id=?", (expired_id,),
        )
        memory_db.commit()
        report = br.build_report(memory_db, run_id)
        assert report["total_trades"] == 2
        assert report["open_trades"] == 1
        assert report["expired_trades"] == 1
        assert report["closed_trades"] == 0
        assert report["winrate"] is None


class TestExportCsv:
    def test_writes_summary_and_breakdown_files(self, memory_db, tmp_path):
        run_id = _seed_run(memory_db)
        trade_id = database.save_backtest_trade(memory_db, run_id, "EUR/USD", _trade())
        memory_db.execute(
            "UPDATE backtest_trades SET status='win', result_pips=40, result_r_multiple=2, "
            "closed_at='2024-01-05T12:00:00+00:00' WHERE id=?", (trade_id,),
        )
        memory_db.commit()
        report = br.build_report(memory_db, run_id)

        out_dir = br.export_csv(report, out_dir=tmp_path)
        assert (out_dir / f"backtest_report_{run_id}_summary.csv").exists()
        assert (out_dir / f"backtest_report_{run_id}_monthly.csv").exists()
        assert (out_dir / f"backtest_report_{run_id}_sessions.csv").exists()
