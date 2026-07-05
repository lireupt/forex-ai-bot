# forex-ai-bot
Testing IA bot

## Validação rápida

```bash
venv/bin/python -m pytest -q
venv/bin/python scripts/evaluate_paper_trades.py
venv/bin/python scripts/check_gates.py --history 10
venv/bin/python scripts/export_logs.py
```

Cron recomendado (apenas 2 jobs). O `main.py` já busca candles e notícias
sozinho, por isso não há scripts de fetch separados. O `run_cycle.sh` corre o
ciclo, exporta o `data.json` **e copia-o para o web root** (`/var/www/forex-bot`)
que o nginx serve — sem este passo o dashboard fica congelado no último export.
Não arquiva a DB (ao contrário do `reset_and_bootstrap.sh`).

```cron
# ciclo + deploy do dashboard, ao minuto 5 de cada hora
5 * * * * cd /root/forex-ai-bot && scripts/run_cycle.sh >> logs/cron.log 2>&1
# limpar logs com mais de 30 dias
30 3 * * * find /root/forex-ai-bot/logs -type f -name "*.log" -mtime +30 -delete
```

## Backtest

Motor de backtest paralelo ao live, que reutiliza a mesma cadeia de decisão
(`modules/decision_engine.decide()`) — a única diferença entre live e
backtest é a origem dos dados. Fase A: sem replay da IA (`ai_score=0`);
valida a espinha dorsal técnica + gating + filtro macro. Só EUR/USD por
agora. Nunca escreve nas tabelas de produção (`paper_trades`, `decisions`)
— usa tabelas próprias (`backtest_runs`, `backtest_decisions`,
`backtest_trades`) isoladas por `run_id`.

**1. Importar histórico** para `market_candles` (formato HistData.com
1-minuto, agregado para 1h, ou CSV OHLCV genérico já na timeframe):

```bash
venv/bin/python scripts/import_history.py --file EURUSD_2024_M1.csv --pair EUR/USD --format histdata1m
venv/bin/python scripts/import_history.py --file eurusd_1h.csv --pair EUR/USD --format ohlcv --timeframe 1h
```

Reporta buracos > 3h fora do fecho semanal normal do forex; nunca inventa
candles para os preencher.

**2. Correr o backtest**, candle a candle, point-in-time estrito:

```bash
venv/bin/python backtest_runner.py --pair EUR/USD --from 2024-01-01 --to 2026-06-30
venv/bin/python backtest_runner.py --pair EUR/USD --from 2024-01-01 --to 2026-06-30 --config overrides.json
```

`overrides.json` pode ter, por exemplo, `{"gating_mode": "score", "apply_spread": true, "sl_mult": 1.0, "tp_mult": 2.0}`.
Imprime o `run_id` no fim — precisas dele para o relatório.

**3. Ler o relatório** de um `run_id`:

```bash
venv/bin/python scripts/backtest_report.py --run-id <run_id>
venv/bin/python scripts/backtest_report.py --run-id <run_id> --csv   # exporta também para logs/
```

Win rate, profit factor, expectância em R, total de pips, max drawdown,
maior sequência de perdas, breakdown mensal, breakdown por sessão (UTC) e
distribuição de `blocking_reason`.

**Teste de equivalência** (backtest vs trades reais do live, para o mesmo
período — requer um snapshot local da DB de produção, nunca liga ao
servidor):

```bash
venv/bin/python scripts/backtest_equivalence.py --pair EUR/USD --from 2026-06-30 --to 2026-07-15 \
    --db /caminho/para/snapshot_producao.db
```

## Fase B (replay de IA histórico) — em progresso

Estende o backtest para incluir a IA real (não `ai_score=0`), via
`ai_result_provider` do `backtest_runner`. Requer notícias e análises de
IA históricas, construídas em dois passos **retomáveis** (cada um tem
limite de quota diária — corre-se uma vez por dia até completar):

**1. Importar notícias históricas** (Alpha Vantage `NEWS_SENTIMENT`,
tier grátis: 25 pedidos/dia por chave). Usa chaves dedicadas via
`ALPHA_VANTAGE_KEY_HISTORICAL` (+ `_2`, `_3`, ... quantas quiseres,
sequenciais) — nunca a `ALPHA_VANTAGE_KEY` do bot ao vivo, para não
competir pela mesma quota:

```bash
venv/bin/python scripts/import_historical_news.py --pair EUR/USD --from 2023-01-01 --to 2025-12-31
```

**2. Construir o cache diário de IA** (Groq tier grátis: 100k tokens/dia
≈ ~25 chamadas/dia a ~3500 tokens/chamada; usa `ai_analyses`, a mesma
tabela de cache do live). Define `GROQ_API_KEY_HISTORICAL` para usar uma
conta dedicada, distinta da `GROQ_API_KEY` do bot ao vivo:

```bash
venv/bin/python scripts/build_historical_ai_cache.py --pair EUR/USD --from 2023-01-01 --to 2025-12-31
```

Ambos imprimem quantos dias/meses faltam — corre de novo no dia seguinte
até "concluído". Não há agendamento automático (agentes cloud não têm
acesso à DB/`.env` locais) — é mesmo preciso correr manualmente uma vez
por dia até o cache estar completo.
