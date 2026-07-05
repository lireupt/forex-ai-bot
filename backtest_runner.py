"""Runner de backtest — Fase A (sem replay de IA, ai_score=0).

Corre candle a candle sobre o histórico já importado em `market_candles`
(ver `scripts/import_history.py`), construindo a cada passo `t` um
`MarketContext` filtrado point-in-time — só vê candles/eventos com
timestamp < t (candles fechadas antes de t) — e chamando
`modules.decision_engine.decide()`, o mesmo
motor de decisão usado pelo live em `main.py`. Quando `trade_allowed`, abre
uma trade virtual resolvida por `modules.trade_simulator` (spread ligado
por omissão, regra SL-primeiro em barra ambígua).

Nunca escreve nas tabelas de produção (`paper_trades`, `decisions`) — só em
`backtest_runs` / `backtest_decisions` / `backtest_trades`, isoladas por
`run_id`.

A componente IA fica desligada nesta fase (`ai_result=None` ->
`decision_engine.DEFAULT_AI_RESULT`, ai_score efectivamente 0). O ponto de
injeção para a Fase B (replay de IA com scores históricos reais) é o
parâmetro `ai_result` do `MarketContext` — ver `run_backtest()`.

Uso:
    python backtest_runner.py --pair EUR/USD --from 2024-01-01 --to 2026-06-30
    python backtest_runner.py --pair EUR/USD --from 2024-01-01 --to 2026-06-30 \
        --config overrides.json
"""

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules import database, decision_engine  # noqa: E402
from modules.pair_spec import get_pair_spec  # noqa: E402
from modules.trade_simulator import simulate_trade  # noqa: E402

load_dotenv()

TIMEFRAMES = {"m15": "15m", "h1": "1h", "h4": "4h", "d1": "1d"}
TIMEFRAME_HOURS = {"15m": 0.25, "1h": 1, "4h": 4, "1d": 24}
LOOKBACK_BARS = 260
PERFORMANCE_LOOKBACK = 200
COOLDOWN_LOOKBACK_HOURS = 24 * 3  # margem generosa acima do dia UTC + cooldown default


def _to_macro_shaped_events(rows):
    """`modules.macro_filter.get_macro_risk()` e `modules.ai_analyst` esperam
    eventos no formato do `ff_calendar` (chaves `date`/`time`/`currency`/
    `event`/`impact`) — se nenhum evento tiver `date`, get_macro_risk()
    ignora silenciosamente o que lhe é passado e vai buscar o calendário
    real de "esta semana" via scrape ao vivo (fuga de futuro num backtest).
    `database.get_high_impact_events()` devolve as linhas cruas da tabela
    (`title`/`country`/`event_time`) — convertidas aqui para o formato
    esperado, sem tocar no formato usado por `decision_engine.resolve_event_gate`
    (que continua a ler as linhas cruas via `ctx.high_impact_events`)."""
    shaped = []
    for row in rows:
        event_time = row.get("event_time") or ""
        shaped.append({
            "date": event_time[:10] if event_time else "",
            "time": event_time,
            "currency": row.get("country") or "",
            "impact": row.get("impact") or "",
            "event": row.get("title") or "",
        })
    return shaped


def _rows_to_df(rows):
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = pd.DataFrame(rows)
    # utc=True tolera candle_time misto (algumas linhas antigas em
    # market_candles, gravadas por outras partes do sistema, não têm
    # offset de fuso) — normaliza tudo para UTC em vez de rebentar.
    df["candle_time"] = pd.to_datetime(df["candle_time"], utc=True, format="mixed")
    df = df.set_index("candle_time")
    return df[["open", "high", "low", "close", "volume"]]


class HistoricalProvider:
    """Fonte de dados point-in-time: filtra candles/eventos por timestamp
    < t (estritamente antes) a partir do que já está em
    `market_candles`/`economic_events`.

    A candle com candle_time == t ainda não fechou nesse instante — só
    fecha no fim do seu próprio período (ex.: a candle "07:00" cobre
    [07:00, 08:00) e só é conhecida por completo às 08:00). Usá-la como
    "preço actual" às 07:00 seria fuga de futuro — por isso o corte é
    estrito (`<`), não `<=`.

    `candle_provider`, se dado, restringe às candles gravadas com essa
    provider tag (ex.: "import") — evita misturar histórico importado com
    o que o live foi cacheando entretanto (fontes/qualidade diferentes)."""

    def __init__(self, conn, pair, candle_provider=None):
        self.conn = conn
        self.pair = pair
        self.candle_provider = candle_provider
        self._high_impact_events = database.get_high_impact_events(conn)
        self._macro_events = _to_macro_shaped_events(self._high_impact_events)

    def candles_up_to(self, timeframe, before_dt, count=LOOKBACK_BARS):
        bar_hours = TIMEFRAME_HOURS.get(timeframe, 1)
        start_dt = before_dt - timedelta(hours=bar_hours * count * 2.5)
        end_dt = before_dt - timedelta(microseconds=1)
        rows = database.get_market_candles_between(
            self.conn, self.pair, timeframe, start_dt.isoformat(), end_dt.isoformat(),
            provider=self.candle_provider,
        )
        return _rows_to_df(rows).tail(count)

    def driving_candles(self, timeframe, date_from, date_to):
        return database.get_market_candles_between(
            self.conn, self.pair, timeframe, date_from, date_to, provider=self.candle_provider,
        )

    def high_impact_events(self):
        # Estático para toda a corrida — os eventos calendarizados são
        # legitimamente conhecidos com antecedência (não é fuga de futuro:
        # é o próprio decide()/macro_filter que decide se estão "perto").
        # Formato cru da tabela — usado por decision_engine.resolve_event_gate.
        return self._high_impact_events

    def macro_events(self):
        # Mesmos eventos, formato ff_calendar — usado por
        # get_macro_risk()/ai_analyst (ctx.events).
        return self._macro_events


class BacktestState:
    """Estado acumulado entre candles — trades e decisões geradas pelo
    próprio backtest, para alimentar cooldown/signal-persistence/
    performance em decide() tal como paper_trades/decisions alimentam o
    live (Passo 3)."""

    def __init__(self):
        self.trades = []
        self.open_trades = []
        self.decisions = []

    def recent_trades_since(self, since_dt):
        return [t for t in self.trades if decision_engine.parse_dt(t["created_at"]) >= since_dt]

    def last_closed_trade(self):
        closed = [t for t in self.trades if t["status"] in ("win", "loss")]
        if not closed:
            return None
        return max(closed, key=lambda t: t["closed_at"] or "")

    def recent_trades_for_performance(self, limit=PERFORMANCE_LOOKBACK):
        return list(reversed(self.trades))[:limit]

    def recent_decisions(self, limit=30):
        return list(reversed(self.decisions))[:limit]


def run_backtest(pair, date_from, date_to, config=None, db_path=None, ai_result_provider=None):
    """`ai_result_provider`, se dado, é `candle_time_iso -> ai_result | None`
    — ponto de injeção para a Fase B (replay de IA com scores históricos
    reais) e para o teste de equivalência (Passo 7), que injeta os
    `ai_score` reais gravados nas decisões live. Sem provider, `ai_result`
    fica None (Fase A, ai_score efectivamente 0)."""
    config = dict(config or {})
    pair_spec = get_pair_spec(pair)
    timeframe = config.get("timeframe", "1h")
    # Mesma resolução que main.py: --config vence, depois env (o que o live
    # realmente usa), depois o default do PairSpec (dentro de
    # compute_trade_levels, quando sl_mult/tp_mult/expiry_bars ficam None).
    gating_mode = config.get("gating_mode") or os.getenv("GATING_MODE") or "score"
    apply_spread = config.get("apply_spread", True)
    sl_mult = config.get("sl_mult")
    if sl_mult is None and os.getenv("PAPER_TRADE_SL_MULT"):
        sl_mult = float(os.getenv("PAPER_TRADE_SL_MULT"))
    tp_mult = config.get("tp_mult")
    if tp_mult is None and os.getenv("PAPER_TRADE_TP_MULT"):
        tp_mult = float(os.getenv("PAPER_TRADE_TP_MULT"))
    expiry_bars = config.get("expiry_bars")
    if expiry_bars is None and os.getenv("PAPER_TRADE_EXPIRY_BARS"):
        expiry_bars = int(float(os.getenv("PAPER_TRADE_EXPIRY_BARS")))
    candle_provider = config.get("candle_provider")

    if db_path:
        database.DB_PATH = Path(db_path)
    conn = database.connect()
    database.init_db(conn)

    provider = HistoricalProvider(conn, pair, candle_provider=candle_provider)
    driving = provider.driving_candles(timeframe, date_from, date_to)
    if not driving:
        conn.close()
        raise ValueError(
            f"Sem candles '{timeframe}' para {pair} entre {date_from} e {date_to}. "
            "Importa histórico primeiro (scripts/import_history.py)."
        )

    run_id = uuid.uuid4().hex
    full_config = {
        "pair": pair, "timeframe": timeframe, "gating_mode": gating_mode,
        "apply_spread": apply_spread, "sl_mult": sl_mult, "tp_mult": tp_mult,
        "expiry_bars": expiry_bars,
    }
    full_config.update(config)
    database.create_backtest_run(conn, run_id, pair, date_from, date_to, full_config)

    state = BacktestState()
    total_decisions = 0
    total_trades = 0

    for row in driving:
        t = decision_engine.parse_dt(row["candle_time"])

        # 1) Construir o MarketContext point-in-time. candles_up_to já exclui
        # a candle em t (só fecha no fim do seu próprio período — usá-la
        # "agora" seria fuga de futuro). A última candle 1h devolvida é a
        # mais recente já fechada, e serve também para resolver trades.
        candles_by_tf = {role: provider.candles_up_to(tf, t) for role, tf in TIMEFRAMES.items()}
        h1_candles = candles_by_tf.get("h1")
        last_closed_h1 = None
        if h1_candles is not None and not h1_candles.empty:
            last_row = h1_candles.iloc[-1]
            last_closed_h1 = {
                "candle_time": h1_candles.index[-1].isoformat(),
                "open": last_row["open"], "high": last_row["high"],
                "low": last_row["low"], "close": last_row["close"], "volume": last_row["volume"],
            }

        # 2) Resolver trades abertas com essa última candle 1h fechada.
        if last_closed_h1 is not None:
            for trade in list(state.open_trades):
                result = simulate_trade(
                    trade, [last_closed_h1], pair_spec, apply_spread=apply_spread, now_dt=t,
                )
                if result is None:
                    continue
                trade["status"] = result.status
                trade["closed_at"] = result.closed_at
                trade["close_price"] = result.close_price
                trade["result_pips"] = result.result_pips
                trade["result_r_multiple"] = result.result_r_multiple
                database.update_backtest_trade_result(conn, trade["_id"], result)
                state.open_trades.remove(trade)

        cooldown_since = t - timedelta(hours=COOLDOWN_LOOKBACK_HOURS)
        ai_result = ai_result_provider(row["candle_time"]) if ai_result_provider else None
        ctx = decision_engine.MarketContext(
            pair=pair,
            timeframe=timeframe,
            now=t,
            pair_spec=pair_spec,
            candles_by_timeframe=candles_by_tf,
            events=provider.macro_events(),
            high_impact_events=provider.high_impact_events(),
            ai_result=ai_result,  # None -> Fase A (decision_engine.DEFAULT_AI_RESULT, ai_score=0)
            news_score=0.0,
            recent_paper_trades=state.recent_trades_since(cooldown_since),
            last_closed_paper_trade=state.last_closed_trade(),
            recent_paper_trades_for_performance=state.recent_trades_for_performance(),
            recent_decisions=state.recent_decisions(),
            gating_mode=gating_mode,
            sl_mult=sl_mult,
            tp_mult=tp_mult,
            expiry_bars=expiry_bars,
            source="backtest",
            operational_now=t,
        )
        decision = decision_engine.decide(ctx)
        total_decisions += 1

        database.save_backtest_decision(conn, run_id, row["candle_time"], {
            "signal": decision.signal,
            "confidence": decision.gating_combined.get("confidence"),
            "combined_score": decision.combined_score,
            "trade_allowed": decision.trade_allowed,
            "block_reason": decision.trade_decision.get("block_reason"),
            "blocking_reason": decision.blocking_reason,
        })
        state.decisions.append({
            "gating_signal": decision.signal,
            "combined_signal": decision.combined.get("signal"),
            "combined_score": decision.combined_score,
            "ai_score": decision.ai_score,
            "multi_timeframe_score": decision.technical_result.get("multi_timeframe_score"),
            "technical_score_h4": decision.technical_result.get("technical_score_h4"),
            "technical_score_d1": decision.technical_result.get("technical_score_d1"),
            "timeframe_alignment": decision.technical_result.get("timeframe_alignment"),
            "trade_allowed": decision.trade_allowed,
        })

        # 3) Abrir trade virtual, se o motor permitir.
        if decision.trade_allowed and decision.trade_params:
            trade = dict(decision.trade_params)
            trade["pair"] = pair
            trade["status"] = "open"
            trade_id = database.save_backtest_trade(conn, run_id, pair, trade)
            trade["_id"] = trade_id
            state.trades.append(trade)
            state.open_trades.append(trade)
            total_trades += 1

    conn.commit()
    database.finish_backtest_run(conn, run_id, len(driving), total_decisions, total_trades)
    conn.close()
    return {
        "run_id": run_id,
        "total_candles": len(driving),
        "total_decisions": total_decisions,
        "total_trades": total_trades,
    }


def to_utc_iso(date_str):
    dt = datetime.fromisoformat(date_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _load_config(path):
    if not path:
        return {}
    with open(path) as f:
        return json.load(f)


def main():
    parser = argparse.ArgumentParser(description="Backtest engine (Fase A — sem IA).")
    parser.add_argument("--pair", default="EUR/USD")
    parser.add_argument("--from", dest="date_from", required=True)
    parser.add_argument("--to", dest="date_to", required=True)
    parser.add_argument(
        "--config", default=None,
        help="JSON com overrides (gating_mode, apply_spread, sl_mult, tp_mult, expiry_bars, timeframe, candle_provider).",
    )
    parser.add_argument(
        "--candle-provider", default=None,
        help="Restringe market_candles a esta provider tag (ex.: 'import') — evita "
             "misturar histórico importado com candles cacheadas pelo live.",
    )
    parser.add_argument(
        "--db", default=None,
        help="SQLite alternativo (default: mesma DB de produção, tabelas backtest_* isoladas).",
    )
    args = parser.parse_args()

    config = _load_config(args.config)
    if args.candle_provider:
        config["candle_provider"] = args.candle_provider
    stats = run_backtest(
        args.pair, to_utc_iso(args.date_from), to_utc_iso(args.date_to),
        config=config, db_path=args.db,
    )
    print(f"[backtest_runner] run_id={stats['run_id']}")
    print(
        f"[backtest_runner] {stats['total_candles']} candles, "
        f"{stats['total_decisions']} decisões, {stats['total_trades']} trades."
    )


if __name__ == "__main__":
    main()
