# Resumo Completo — Forex AI Bot

**Data:** 2026-06-15  
**Projeto:** Paper-trading algorítmico EUR/USD com IA  
**Owner:** lireu.pt@gmail.com  
**Repo:** `/home/lireupt/Projects/forex-ai-bot`  
**Produção:** VPS Ubuntu `38.19.202.171`, dashboard em `http://38.19.202.171`

---

## 1. O Que É Este Projecto

Um bot de **paper-trading** (simulado, sem dinheiro real) que analisa o par EUR/USD em timeframe 1h e decide automaticamente se deve abrir uma posição BUY, SELL ou ficar NEUTRAL. Corre em cron a cada hora no servidor VPS.

**Objectivo de investigação:** perceber se uma combinação de análise técnica clássica + LLM (IA) consegue gerar sinais de trading com win-rate e profit-factor positivos, de forma sistemática e auditável.

---

## 2. Arquitectura do Ciclo Principal

```
main.py  (cron: 5 * * * *)
│
├── [1/4] Notícias   → RSS feeds + HTML scrape (Investing) + APIs (AlphaVantage, Marketaux)
├── [2/4] Calendário → FXStreet + RSS económico + filtro macro
├── [3/4] Técnica    → Multi-timeframe: M15 + H1 + H4 + D1
│         └── RSI(14) + EMA(20,50) + MACD + ATR(14) + ADX
├── [4/4] IA         → Groq (llama-3.3-70b-versatile) analisa notícias + técnica
│
├── Combinação de sinais → score ponderado (IA 30% + técnica 55% + news 15%)
├── Gating → AdaptiveRiskEngine decide se abre trade e com que tamanho
├── Paper trade → registo em SQLite (entry, SL, TP, expiry)
├── Camada 4 (SHADOW) → IA agregadora lê snapshot completo (não influencia decisão)
├── Camada 5 → Rolling Market Context (memória contextual contínua)
└── Export → web/data.json → /var/www/forex-bot/ → dashboard nginx
```

---

## 3. Módulos Principais

| Ficheiro | Responsabilidade |
|---|---|
| `main.py` | Orquestrador do ciclo completo |
| `modules/technical.py` | RSI, EMA, MACD, ATR, ADX → sinal técnico + shadow |
| `modules/multi_timeframe.py` | Agrega M15/H1/H4/D1 → multi_timeframe_score |
| `modules/ai_analyst.py` | Chama Groq, devolve sinal + confiança + reasoning |
| `modules/scoring.py` | Combina scores (ai + técnica + news) → combined_score |
| `modules/risk_engine.py` | AdaptiveRiskEngine: decide execução + risk_multiplier |
| `modules/risk.py` | Avalia trade: SL/TP/ATR, eventos, cooldown |
| `modules/rolling_context.py` | Memória contextual: compara ciclo actual com anteriores |
| `modules/ai_aggregator.py` | Camada 4 shadow: voto agregado (apenas observador) |
| `modules/news_scraper.py` | Busca notícias de múltiplas fontes |
| `modules/price_feed.py` | Candles via Yahoo Finance |
| `modules/database.py` | SQLite — 9 tabelas, schema aditivo |
| `modules/context_snapshot.py` | Constrói snapshot completo para o agregador/rolling |
| `modules/macro_filter.py` | Filtra eventos de alto impacto no calendário |
| `modules/weekly_market_prep.py` | Preparação semanal ao domingo |

**Scripts utilitários:**
- `scripts/run_cycle.sh` — wrapper do cron (main + evaluate + check + export + cp para nginx)
- `scripts/evaluate_paper_trades.py` — fecha trades abertas por SL/TP/expiry
- `scripts/check_gates.py` — avalia qualidade do gate (trades suficientes, win-rate, PF)
- `scripts/export_logs.py` — gera `web/data.json` para o dashboard
- `scripts/calibration_report.py` — relatório de calibração (inclui rolling context)

---

## 4. Base de Dados SQLite (`data/forex_bot.db`)

9 tabelas:

| Tabela | Conteúdo |
|---|---|
| `decisions` | Cada ciclo: todos os sinais, scores, blocking_reason |
| `paper_trades` | Trades simuladas: entry, SL, TP, status (open/win/loss/expired) |
| `market_candles` | OHLCV por timeframe e provider |
| `news_items` | Artigos filtrados por par |
| `economic_events` | Eventos do calendário económico |
| `ai_analyses` | Cache de respostas da IA (hash do input) |
| `gate_checks` | Histórico de verificações de qualidade |
| `rolling_market_context` | Memória contextual contínua (21 colunas) |
| `weekly_market_prep` | Preparação macro semanal |

**Estado actual (2026-06-15):** 5 decisões, 0 paper trades (base de dados quase vazia — a engine ainda está a acumular dados de baseline).

---

## 5. Lógica de Sinais — Como o Bot Decide

### 5.1 Sinal Técnico (multi-timeframe)
- **M15, H1, H4, D1** — cada timeframe corre RSI + EMA + MACD separadamente
- H1 é o timeframe principal; D1 é filtro de tendência maior
- `multi_timeframe_score` = média ponderada dos scores por timeframe
- **Shadow técnico**: versão paralela com regra menos estrita (2/3 votos), apenas para benchmarking

### 5.2 Sinal IA (fundamental)
- Groq recebe: últimas notícias + calendário económico + snapshot técnico
- Devolve: `signal` (BUY/SELL/NEUTRAL) + `confidence` (0-100) + `bias` + `reasoning`
- Se `confidence < 35` (AI_VOTE_MIN_CONFIDENCE), a IA **abstém-se** — o seu peso é retirado e renormalizado para a técnica
- `ai_score` = direção × magnitude do `confidence_adjustment` (max ±0.25)

### 5.3 Score Combinado
```
combined_score = ai_score × 0.30 + technical_score × 0.55 + news_score × 0.15
```
- Se IA se abstém: `combined_score = technical_score × (0.55/0.85) + news_score × (0.15/0.85)`
- `BUY` se `combined_score >= 0.42`
- `SELL` se `combined_score <= -0.42`
- Caso contrário: `NEUTRAL` (bloqueado)

### 5.4 AdaptiveRiskEngine (Gate de Execução)
Substituiu o antigo `MIN_CONFIDENCE=65` binário (que nunca passava).

- Threshold dinâmico entre `ADAPTIVE_MIN_FLOOR=0.35` e `ADAPTIVE_MIN_CEILING=0.65`
- Bónus/penalizações auditáveis: alignment multi-TF, evento próximo, cooldown, loss streak
- `risk_multiplier`: micro / small / normal / full
- NEUTRAL continua sempre bloqueado; direcional válido = `|combined_score| >= 0.35`

### 5.5 Regras Adicionais de Bloqueio
- Eventos de **alto impacto** ±30 min → bloqueio duro
- Eventos de **médio impacto** → reduz confiança × 0.80
- **Cooldown:** 90 min após trade na mesma direcção
- **Max 2 sinais/dia** por direcção
- **Spread > 2.5 pips** → bloqueio
- **ATR > 35 pips** → volatilidade extrema, bloqueio
- **Horário operacional:** 07:00-15:00 UTC
- **Fim-de-semana:** modo leve (sem trades)

---

## 6. Camadas de IA

O bot tem **4 camadas de IA**, com papéis distintos:

| Camada | Módulo | Papel | Influencia trade? |
|---|---|---|---|
| 1 — IA Fundamental | `ai_analyst.py` | Analisa notícias/macro, devolve sinal | **Sim** (30% do score) |
| 2 — News Score | `scoring.py` | Score de sentimento das notícias | **Sim** (15% do score) |
| 3 — AdaptiveRiskEngine | `risk_engine.py` | Decide execução e sizing | **Sim** (gate final) |
| 4 — IA Agregadora | `ai_aggregator.py` | Lê snapshot completo, voto agregado | **Não** (shadow only) |
| 5 — Rolling Context | `rolling_context.py` | Memória contextual contínua | **Não** (contexto only) |

---

## 7. Infra-estrutura / Deploy

**Topologia:**
```
[cron VPS] → run_cycle.sh → main.py → SQLite (data/forex_bot.db)
                                    → web/data.json
                                    → cp → /var/www/forex-bot/data.json
                                                  ↑
                                           nginx serve → http://38.19.202.171
```

**Gotcha crítico:** Existem duas cópias de `data.json`. Se o cron não fizer o `cp`, o dashboard fica congelado. O `run_cycle.sh` trata disto.

**Cron (2 jobs apenas):**
```cron
5 * * * *   scripts/run_cycle.sh >> logs/cron.log 2>&1
30 3 * * *  find logs -mtime +30 -delete
```

---

## 8. Variáveis de Ambiente Relevantes (`.env`)

| Variável | Valor | Descrição |
|---|---|---|
| `AI_PROVIDER` | `groq` | LLM usado (Groq llama-3.3-70b) |
| `DRY_RUN` | `True` | Apenas paper trading (sem real) |
| `TIMEFRAME` | `1h` | Timeframe principal |
| `COMBINED_BUY_THRESHOLD` | `0.42` | Limiar para sinal BUY |
| `COMBINED_SELL_THRESHOLD` | `-0.42` | Limiar para sinal SELL |
| `ADAPTIVE_BASE_MIN_CONFIDENCE` | `0.45` | Threshold base do AdaptiveRisk |
| `AI_VOTE_MIN_CONFIDENCE` | `35` | IA abstém-se abaixo disto |
| `COOLDOWN_MINUTES` | `90` | Cooldown entre trades |
| `MAX_DIRECTION_SIGNALS_PER_DAY` | `2` | Max trades/dia por direcção |
| `ROLLING_CONTEXT_ENABLED` | `True` | Memória contextual activa |
| `AI_AGGREGATOR_ENABLED` | `False` | IA agregadora desligada |
| `USE_ATR_SL_TP` | `True` | SL/TP dinâmicos via ATR |
| `ATR_SL_MULT` | `1.5` | SL = ATR × 1.5 |
| `ATR_TP_MULT` | `3.0` | TP = ATR × 3.0 (RR = 1:2) |

---

## 9. Roadmap e Estado Actual

### Fase actual: Fase 1 — Acumulação de Baseline
- A `AdaptiveRiskEngine` está activa em paper trading (DRY_RUN)
- Objectivo: 30-50 paper trades com resultados reais
- A IA agregadora (Camada 4) está desligada para não poluir a baseline

### Próximas Fases (planeadas pelo user):
- **Fase 2:** Após 30-50 trades, activar a IA como *juíza de timing* (não veto/decisor), primeiro em shadow mode
- **Fase 3:** Comparar win-rate / profit-factor / drawdown vs baseline antes de qualquer promoção
- **Regra:** IA só ganha autoridade de execução se bater a baseline — nunca antes

### Estado da DB hoje:
- 5 decisões registadas
- 0 paper trades (a engine ainda não disparou ou os ciclos ainda são poucos)

---

## 10. Ficheiros de Diagnóstico / Qualidade

```bash
# Ver últimas decisões
sqlite3 data/forex_bot.db "SELECT timestamp, combined_signal, combined_score, block_reason FROM decisions ORDER BY timestamp DESC LIMIT 10;"

# Ver paper trades
sqlite3 data/forex_bot.db "SELECT * FROM paper_trades ORDER BY created_at DESC LIMIT 10;"

# Relatório de calibração completo
venv/bin/python scripts/calibration_report.py

# Fechar trades abertas manualmente
venv/bin/python scripts/evaluate_paper_trades.py

# Ver qualidade do gate
venv/bin/python scripts/check_gates.py --history 20

# Forçar ciclo completo (refresh de cache)
FORCE_REFRESH=True venv/bin/python main.py
```

---

## 11. Perguntas Abertas / Áreas a Analisar

1. **Porque é que há só 5 decisões e 0 trades?** O bot está a correr correctamente no servidor? Verificar `logs/cron.log` no VPS.

2. **O threshold `0.42` está bem calibrado?** Com só 5 decisões é difícil saber. O relatório de calibração mostra o histograma de `combined_score`.

3. **A IA está a abstrair-se muito?** Se `AI_VOTE_MIN_CONFIDENCE` de 35% faz a IA abstrair-se frequentemente, o sinal técnico domina — verificar nos `decisions` quantas vezes `ai_confidence < 35`.

4. **O Rolling Context está a ser útil?** Activado em 2026-06-07, mas com só 5 ciclos é muito cedo para avaliar.

5. **Quando activar a Camada 4 (IA Agregadora)?** Só faz sentido quando houver pelo menos 20-30 decisões para o snapshot de performance ser significativo.

6. **Risk/Reward de 1:2 (ATR×1.5 / ATR×3.0) é adequado para EUR/USD 1h?** Depende do win-rate alcançado. Com win-rate de 40% e RR de 1:2 já é positivo (expectancy > 0).
