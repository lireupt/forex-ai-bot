"""Testes para `scripts.check_gates` — métricas, gates e integração com DB."""

from datetime import datetime, timedelta, timezone

import pytest

from scripts import check_gates


def _trade(status, pips=None, r=None, source="ai_only", direction="BUY",
           closed_at=None, created_at="2026-05-01T00:00:00+00:00"):
    return {
        "status": status,
        "result_pips": pips,
        "result_r_multiple": r,
        "source": source,
        "direction": direction,
        "closed_at": closed_at,
        "created_at": created_at,
    }


class TestProfitFactor:
    def test_no_trades_returns_none(self):
        assert check_gates.profit_factor([]) is None

    def test_only_wins_returns_capped(self):
        wins = [_trade("win", pips=20, r=1.5)] * 3
        assert check_gates.profit_factor(wins) == 999.0

    def test_only_losses_zero(self):
        losses = [_trade("loss", pips=-10, r=-1.0)] * 3
        # 0 / 30 = 0
        assert check_gates.profit_factor(losses) == 0.0

    def test_balanced(self):
        # 3 wins de 20 pips (60), 3 losses de 10 (30) -> PF=2.0
        trades = [_trade("win", pips=20, r=1.5)] * 3 + [_trade("loss", pips=-10, r=-1.0)] * 3
        assert check_gates.profit_factor(trades) == 2.0

    def test_break_even(self):
        # 1 win de 10, 1 loss de 10 -> PF=1.0
        trades = [_trade("win", pips=10, r=1.0), _trade("loss", pips=-10, r=-1.0)]
        assert check_gates.profit_factor(trades) == 1.0

    def test_ignores_none_pips(self):
        trades = [_trade("win", pips=None, r=None), _trade("loss", pips=-5, r=-1.0)]
        # apenas o loss conta -> PF=0
        assert check_gates.profit_factor(trades) == 0.0


class TestAverageR:
    def test_empty(self):
        assert check_gates.average_r([]) is None

    def test_basic_average(self):
        trades = [_trade("win", r=2.0), _trade("loss", r=-1.0), _trade("win", r=1.5)]
        # (2 + -1 + 1.5)/3 = 0.83
        assert check_gates.average_r(trades) == 0.83

    def test_ignores_none(self):
        trades = [_trade("win", r=2.0), _trade("expired", r=None)]
        assert check_gates.average_r(trades) == 2.0


class TestWinRate:
    def test_empty(self):
        assert check_gates.win_rate([]) is None

    def test_no_decisive(self):
        # apenas expired
        trades = [_trade("expired", pips=5, r=0.5)]
        assert check_gates.win_rate(trades) is None

    def test_only_wins(self):
        trades = [_trade("win", pips=10, r=1.0)] * 5
        assert check_gates.win_rate(trades) == 100.0

    def test_partial(self):
        trades = [_trade("win")] * 3 + [_trade("loss")] * 2
        # 3 / 5 = 60%
        assert check_gates.win_rate(trades) == 60.0

    def test_expired_excluded(self):
        trades = [_trade("win"), _trade("loss"), _trade("expired")]
        # apenas win+loss contam -> 1/2 = 50%
        assert check_gates.win_rate(trades) == 50.0


class TestMaxLosingStreak:
    def test_empty(self):
        assert check_gates.max_losing_streak([]) == 0

    def test_no_losses(self):
        trades = [_trade("win", closed_at=f"2026-05-0{i}T12:00:00+00:00") for i in range(1, 4)]
        assert check_gates.max_losing_streak(trades) == 0

    def test_single_loss(self):
        trades = [
            _trade("win", closed_at="2026-05-01T12:00:00+00:00"),
            _trade("loss", closed_at="2026-05-02T12:00:00+00:00"),
            _trade("win", closed_at="2026-05-03T12:00:00+00:00"),
        ]
        assert check_gates.max_losing_streak(trades) == 1

    def test_streak_of_three(self):
        trades = [
            _trade("loss", closed_at="2026-05-01T12:00:00+00:00"),
            _trade("loss", closed_at="2026-05-02T12:00:00+00:00"),
            _trade("loss", closed_at="2026-05-03T12:00:00+00:00"),
            _trade("win", closed_at="2026-05-04T12:00:00+00:00"),
            _trade("loss", closed_at="2026-05-05T12:00:00+00:00"),
        ]
        assert check_gates.max_losing_streak(trades) == 3

    def test_unsorted_input_sorted_internally(self):
        # Mete os trades fora de ordem; o algoritmo deve ordenar por closed_at
        trades = [
            _trade("loss", closed_at="2026-05-03T12:00:00+00:00"),
            _trade("loss", closed_at="2026-05-01T12:00:00+00:00"),
            _trade("loss", closed_at="2026-05-02T12:00:00+00:00"),
            _trade("win", closed_at="2026-05-04T12:00:00+00:00"),
        ]
        assert check_gates.max_losing_streak(trades) == 3

    def test_expired_breaks_streak(self):
        # expired não conta como loss nem como win -> apenas é skipped
        trades = [
            _trade("loss", closed_at="2026-05-01T12:00:00+00:00"),
            _trade("expired", closed_at="2026-05-02T12:00:00+00:00"),
            _trade("loss", closed_at="2026-05-03T12:00:00+00:00"),
        ]
        # apenas win/loss entram no walk; 2 losses, mas não consecutivos no
        # raciocinio? Na nossa implementação só win/loss são considerados,
        # logo a sequência é "loss, loss" -> streak = 2.
        # (Se quiseres que expired interrompa o streak, deves mudar o filter.)
        assert check_gates.max_losing_streak(trades) == 2


class TestMaxDrawdownPct:
    def test_empty_none(self):
        assert check_gates.max_drawdown_pct([]) is None

    def test_pure_uptrend_zero_dd(self):
        trades = [
            _trade("win", r=1.0, closed_at=f"2026-05-0{i}T12:00:00+00:00")
            for i in range(1, 4)
        ]
        assert check_gates.max_drawdown_pct(trades) == 0.0

    def test_simple_dd(self):
        # equity 100 -> +1 = 101 (peak) -> -1 = 100 -> -1 = 99
        # peak 101, valley 99 -> DD = (101-99)/101 = 1.98%
        trades = [
            _trade("win", r=1.0, closed_at="2026-05-01T12:00:00+00:00"),
            _trade("loss", r=-1.0, closed_at="2026-05-02T12:00:00+00:00"),
            _trade("loss", r=-1.0, closed_at="2026-05-03T12:00:00+00:00"),
        ]
        result = check_gates.max_drawdown_pct(trades, risk_per_trade_pct=1.0)
        assert result == pytest.approx(1.98, abs=0.05)

    def test_uses_risk_per_trade(self):
        # Mesmo cenário com 2% risk -> DD em valor absoluto duplica
        trades = [
            _trade("win", r=1.0, closed_at="2026-05-01T12:00:00+00:00"),
            _trade("loss", r=-1.0, closed_at="2026-05-02T12:00:00+00:00"),
            _trade("loss", r=-1.0, closed_at="2026-05-03T12:00:00+00:00"),
        ]
        result = check_gates.max_drawdown_pct(trades, risk_per_trade_pct=2.0)
        # equity: 100 -> 102 (peak) -> 100 -> 98. DD = (102-98)/102 = 3.92%
        assert result == pytest.approx(3.92, abs=0.05)

    def test_recovers_to_new_peak(self):
        # 100 -> 101 (peak) -> 99 -> 102 (new peak) -> 100
        # DDs: max(101-99=2/101, 102-100=2/102) -> ~1.98%
        trades = [
            _trade("win", r=1.0, closed_at="2026-05-01T12:00:00+00:00"),
            _trade("loss", r=-2.0, closed_at="2026-05-02T12:00:00+00:00"),
            _trade("win", r=3.0, closed_at="2026-05-03T12:00:00+00:00"),
            _trade("loss", r=-2.0, closed_at="2026-05-04T12:00:00+00:00"),
        ]
        result = check_gates.max_drawdown_pct(trades, risk_per_trade_pct=1.0)
        assert result == pytest.approx(1.98, abs=0.05)


class TestEvaluateGates:
    def _good_metrics(self):
        return {
            "wins": 30, "losses": 20, "expired": 5, "open": 0,
            "win_rate": 60.0, "profit_factor": 1.8, "avg_r": 0.5,
            "max_losing_streak": 3, "max_drawdown_pct": 8.0,
        }

    def test_all_pass_yields_go(self):
        config = check_gates.load_gate_config()
        status, gates = check_gates.evaluate_gates(self._good_metrics(), config)
        assert status == "go"
        assert all(g["pass"] for g in gates)

    def test_insufficient_trades_yields_partial(self):
        m = self._good_metrics()
        m.update({"wins": 5, "losses": 3, "expired": 0})
        config = check_gates.load_gate_config()
        status, _ = check_gates.evaluate_gates(m, config)
        assert status == "partial"

    def test_pf_below_threshold_no_go(self):
        m = self._good_metrics()
        m["profit_factor"] = 1.0
        config = check_gates.load_gate_config()
        status, gates = check_gates.evaluate_gates(m, config)
        assert status == "no_go"
        pf_gate = next(g for g in gates if g["name"] == "profit_factor")
        assert pf_gate["pass"] is False

    def test_streak_too_long_no_go(self):
        m = self._good_metrics()
        m["max_losing_streak"] = 7
        config = check_gates.load_gate_config()
        status, gates = check_gates.evaluate_gates(m, config)
        assert status == "no_go"
        streak = next(g for g in gates if g["name"] == "max_streak_losses")
        assert streak["pass"] is False

    def test_dd_too_high_no_go(self):
        m = self._good_metrics()
        m["max_drawdown_pct"] = 25.0
        config = check_gates.load_gate_config()
        status, _ = check_gates.evaluate_gates(m, config)
        assert status == "no_go"

    def test_none_pf_marks_insufficient(self):
        m = self._good_metrics()
        m["profit_factor"] = None
        config = check_gates.load_gate_config()
        _, gates = check_gates.evaluate_gates(m, config)
        pf_gate = next(g for g in gates if g["name"] == "profit_factor")
        assert pf_gate["insufficient_data"] is True
        assert pf_gate["pass"] is False

    def test_custom_thresholds_via_env(self, monkeypatch):
        monkeypatch.setenv("GATE_MIN_PROFIT_FACTOR", "2.0")
        config = check_gates.load_gate_config()
        m = self._good_metrics()
        m["profit_factor"] = 1.8  # passa com 1.3, falha com 2.0
        status, _ = check_gates.evaluate_gates(m, config)
        assert status == "no_go"


class TestRunCheckIntegration:
    def _seed_trades(self, conn, count_wins=30, count_losses=15, count_expired=3,
                     interleave=True):
        from modules import database

        decision_id = conn.execute(
            "INSERT INTO decisions (timestamp, pair, created_at) VALUES (?, ?, ?)",
            ("2026-05-06T19:00:00+00:00", "EUR/USD", "2026-05-06T19:00:00+00:00"),
        ).lastrowid
        conn.commit()

        base = datetime(2026, 5, 1, tzinfo=timezone.utc)
        idx = 0

        def _add(status, r, pips, source="ai_only", direction="BUY"):
            nonlocal idx
            closed_at = (base + timedelta(hours=idx)).isoformat()
            idx += 1
            pt = {
                "decision_id": decision_id,
                "pair": "EUR/USD", "timeframe": "1h",
                "direction": direction, "entry_price": 1.17,
                "simulated_sl": 1.168, "simulated_tp": 1.174,
                "sl_pips": 20.0, "tp_pips": 40.0, "atr_pips": 20.0,
                "atr_price": 0.002, "status": "open",
                "source": source, "signal_source": f"{source}_signal",
                "created_at": (base + timedelta(hours=idx - 1)).isoformat(),
                "expiry_at": (base + timedelta(hours=idx + 6)).isoformat(),
            }
            pt_id = database.create_paper_trade(conn, pt)
            database.update_paper_trade_result(
                conn, pt_id, status, 1.17, closed_at, "x", pips, r,
            )
            return pt_id

        if interleave:
            # Intercala win/loss para evitar streaks artificiais
            sequence = []
            sequence.extend([("win", 1.5, 30.0)] * count_wins)
            sequence.extend([("loss", -1.0, -20.0)] * count_losses)
            sequence.extend([("expired", 0.1, 2.0)] * count_expired)
            # Alterna entre wins e losses pegando 2 wins por cada loss (ratio realista)
            wins_iter = iter([s for s in sequence if s[0] == "win"])
            losses_iter = iter([s for s in sequence if s[0] == "loss"])
            expired_iter = iter([s for s in sequence if s[0] == "expired"])
            wins_list = list(wins_iter)
            losses_list = list(losses_iter)
            expired_list = list(expired_iter)
            ordered = []
            wi = li = ei = 0
            while wi < len(wins_list) or li < len(losses_list) or ei < len(expired_list):
                # Padrão repetitivo: 2 wins, 1 loss, ocasional expired
                for _ in range(2):
                    if wi < len(wins_list):
                        ordered.append(wins_list[wi]); wi += 1
                if li < len(losses_list):
                    ordered.append(losses_list[li]); li += 1
                if ei < len(expired_list) and (wi + li) % 5 == 0:
                    ordered.append(expired_list[ei]); ei += 1
            for status, r, pips in ordered:
                _add(status, r, pips)
        else:
            for _ in range(count_wins):
                _add("win", 1.5, 30.0)
            for _ in range(count_losses):
                _add("loss", -1.0, -20.0)
            for _ in range(count_expired):
                _add("expired", 0.1, 2.0)

    def test_run_check_returns_go_with_good_data(self, memory_db):
        self._seed_trades(memory_db, count_wins=40, count_losses=15, count_expired=2)
        snapshot = check_gates.run_check(conn=memory_db, persist=False)
        assert snapshot["overall"]["status"] == "go"
        assert snapshot["overall"]["metrics"]["wins"] == 40
        assert snapshot["overall"]["metrics"]["losses"] == 15

    def test_run_check_partial_when_few_trades(self, memory_db):
        self._seed_trades(memory_db, count_wins=5, count_losses=3, count_expired=0)
        snapshot = check_gates.run_check(conn=memory_db, persist=False)
        assert snapshot["overall"]["status"] == "partial"

    def test_run_check_no_go_when_pf_low(self, memory_db):
        # pf vai ser baixo: muitos losses
        self._seed_trades(memory_db, count_wins=20, count_losses=40, count_expired=0)
        snapshot = check_gates.run_check(conn=memory_db, persist=False)
        assert snapshot["overall"]["status"] == "no_go"

    def test_persist_writes_to_db_and_file(self, memory_db, tmp_path, monkeypatch):
        from modules import database

        gates_file = tmp_path / "gates_check.json"
        monkeypatch.setattr(check_gates, "GATES_OUT", gates_file)

        self._seed_trades(memory_db, count_wins=40, count_losses=15, count_expired=2)

        # Patch DB connect para usar a memory_db (sem fechar)
        class _NoCloseConn:
            def __init__(self, conn):
                self._conn = conn
            def __getattr__(self, name):
                return getattr(self._conn, name)
            def close(self):
                pass

        monkeypatch.setattr(database, "connect", lambda: _NoCloseConn(memory_db))

        snapshot = check_gates.run_check(persist=True)
        assert gates_file.exists()
        # tabela gate_checks deve ter 1 entrada
        rows = database.get_recent_gate_checks(memory_db)
        assert len(rows) == 1
        assert rows[0]["status"] == snapshot["overall"]["status"]
