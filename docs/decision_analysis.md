# Decision analysis — Forex AI Bot

Este documento descreve como cada decisão é construída, como é classificada,
e como interpretar a tabela / modal "Detalhes" do dashboard. Nada aqui muda o
comportamento do bot em produção — o gating real continua a usar a regra
estrita combinada (3/3 votos técnicos + IA). Os scores e paper trades servem
para análise, observabilidade e simulação.

## Componentes do sinal

### AI signal
- Vem do módulo `modules/ai_analyst.py`, que envia notícias, eventos
  económicos **e um snapshot técnico actual** (RSI, EMA20/50, MACD, ATR,
  resumo do sinal técnico) a um LLM (Groq ou Claude) e pede um JSON com
  `signal`, `confidence`, `reasoning`, `risk_level`, `hold_off`.
- O snapshot técnico foi adicionado para a IA poder integrar fundamental
  + técnica numa só decisão. O `system prompt` instrui a IA a referir
  ambos na razão e a baixar `confidence` quando há contradição em vez de
  marcar `hold_off` por defeito.
- Como o `input_hash` da AI cache inclui agora o snapshot técnico, sempre
  que os indicadores mudam significativamente a IA volta a analisar (não
  fica presa numa decisão diária estática).
- Em SQLite guardamos:
  - `ai_signal` (BUY / SELL / NEUTRAL)
  - `ai_confidence_score` (0..1, ou seja `confidence/100`)
  - `ai_score` (-1..+1) — `direção × confidence` (ex.: BUY 70 → +0.70)
  - `ai_reason` — `reasoning` da resposta da IA
  - `ai_features_snapshot` — JSON com close, ATR, RSI, EMA, MACD,
    volatilidade, candles recentes etc., para perceber em que dados a IA
    estava a "olhar"
  - `ai_model_version` — ex.: `groq:llama-3.3-70b-versatile`

### Technical signal
- Vem de `modules/technical.py`, baseado em três indicadores: RSI(14),
  EMA20/EMA50, MACD(12/26/9). Cada indicador vota `bullish`, `bearish` ou
  `neutral`.
- A regra estrita (`technical_signal`) só dá BUY/SELL com **3/3 votos
  alinhados**, caso contrário NEUTRAL.
- Guardamos:
  - `technical_signal` (label estrito)
  - `technical_score` (-1..+1) = (bullish_votes − bearish_votes) / 3
  - `technical_reason` — descrição compacta dos votos.

### Shadow signal
- Mesma análise técnica mas com regra mais permissiva: BUY/SELL com
  **2/3 votos alinhados** sem voto contrário.
- Permite ver o que a estratégia *daria* com critério mais aberto sem
  alterar o gating real.
- Guardamos `shadow_technical_signal`, `shadow_technical_confidence`,
  `shadow_technical_reason`, e um `shadow_score` (sinal × confidence/100).
- Existe ainda um `shadow_combined_signal` que mistura o `ai_signal`
  com o `shadow_technical_signal` (regra mais flexível do que a estrita).

### Combined signal
- Versão estrita: `combined_signal` é BUY/SELL apenas se IA e técnica
  concordarem em BUY ou SELL; caso contrário NEUTRAL. Esta é a regra que
  decide se o trade dry-run é simulado.
- Versão por score: `combined_score` é a média ponderada de `ai_score`,
  `technical_score` (e opcionalmente `shadow_score` se o peso for > 0).
  Pesos default: 0.6 IA + 0.4 técnica. Ajustável por env vars
  `SCORE_AI_WEIGHT`, `SCORE_TECHNICAL_WEIGHT`, `SCORE_SHADOW_WEIGHT`.
- A label derivada do score (`score_combined_signal`) usa thresholds
  configuráveis:
  - `combined_score >= SCORE_BUY_THRESHOLD` → BUY (default `+0.35`)
  - `combined_score <= SCORE_SELL_THRESHOLD` → SELL (default `-0.35`)
  - caso contrário NEUTRAL
- O campo `combined_reason` traz uma descrição em texto de como se chegou
  ao resultado: scores, concordância/discordância e qual regra ditou o
  label estrito.

## Por que razão um trade é bloqueado

Um trade só é simulado (`trade_allowed = true`) se:
1. `DRY_RUN = True` (modo simulado activo)
2. O sinal **de gating** é BUY ou SELL (qual depende de `GATING_MODE`)
3. `hold_off` da IA é falso
4. `confidence` ≥ `MIN_CONFIDENCE` (default 65)
5. há `current_price` válido
6. não há evento high-impact próximo (configurável)

Se algum critério falhar, gravamos `blocking_reason` (e o histórico
`block_reason`) com a razão exacta. As mais comuns:
- `sinal combinado é NEUTRAL` — IA e técnica não concordam em BUY/SELL
- `confiança X% abaixo do mínimo` — confidence baixa
- `hold_off ativo` — a IA pediu prudência
- `evento high impact …` — evento económico próximo

### Modos de gating (`GATING_MODE`)

A regra que determina se um trade pode ser simulado é configurável.
**Default `strict` — comportamento conservador, igual ao histórico**:

| Modo | Origem do sinal | Quando faz sentido |
|---|---|---|
| `strict` (default) | `combined_signal` (3/3 IA + técnica) | Fase de testes / quando ainda não temos paper trades validados |
| `score` | `score_combined_signal` (threshold sobre `combined_score`) | Depois de o threshold ter sido validado por uma amostra de paper trades |
| `shadow` | `shadow_combined_signal` (IA + técnica 2/3) | Para testar sinais mais permissivos sem mexer no threshold |

Em **todos os modos** o `hold_off` da IA continua a ser respeitado — se a IA
sinalizar evento iminente / contradições fortes, o trade é bloqueado mesmo
que o gating dê BUY/SELL. Isto é deliberado: garante que mudanças de gating
nunca contornam o sinal de risco da IA.

Para experimentar:
```bash
GATING_MODE=score python main.py
GATING_MODE=shadow python main.py
```

A decisão guarda em SQLite os campos `gating_mode`, `gating_signal` e
`gating_confidence` para podermos ver a posteriori que regra esteve activa.

## Numeric scoring (resumo)

| Field | Range | Significado |
|---|---|---|
| `ai_score` | -1.0 .. +1.0 | sentido × confidence/100 |
| `ai_confidence_score` | 0..1 | confidence / 100 |
| `technical_score` | -1.0 .. +1.0 | (bullish - bearish) / 3 |
| `shadow_score` | -1.0 .. +1.0 | sinal shadow × confidence/100 |
| `combined_score` | -1.0 .. +1.0 | média ponderada (default 0.6 IA + 0.4 técnica) |

Negativo = pressão SELL, positivo = pressão BUY, ~0 = NEUTRAL.

## Paper trades

Sempre que o `ai_signal` ou o `combined_signal` é BUY/SELL, criamos uma
paper trade (mesmo que o gating real esteja bloqueado). Cada paper trade
guarda:

- `decision_id`, `pair`, `timeframe`, `direction`
- `entry_price` (preço actual no momento)
- `simulated_sl` / `simulated_tp` (níveis em preço)
- `sl_pips`, `tp_pips` (distância em pips)
- `atr_pips` / `atr_price` usados
- `expiry_at` — após N candles do timeframe (default 6 candles 1h)
- `source` — `ai_only` (apenas a IA disse BUY/SELL) ou `combined`
- `status` — `open`, `win`, `loss`, `expired`
- `close_price`, `closed_at`, `close_reason`
- `result_pips`, `result_r_multiple`

Defaults configuráveis por env:
- `PAPER_TRADE_SL_MULT` (default 1.0 × ATR)
- `PAPER_TRADE_TP_MULT` (default 2.0 × ATR)
- `PAPER_TRADE_EXPIRY_BARS` (default 6 barras do timeframe)

### Avaliação de paper trades

`scripts/evaluate_paper_trades.py` percorre as paper trades em aberto e:
1. Lê as candles entre `created_at` e `expiry_at` da tabela `market_candles`
2. Para cada candle:
   - se TP foi tocado (high ≥ TP em BUY ou low ≤ TP em SELL) → `win`
   - se SL foi tocado primeiro → `loss`
   - se ambos na mesma candle, marca `loss` (assume SL primeiro — modo
     conservador, podes mudar mais tarde)
3. Se a expiry chegou sem SL/TP, marca `expired` com o close da última
   candle disponível.

Corre periodicamente (por exemplo num cron, depois do `main.py`):

```bash
python scripts/evaluate_paper_trades.py
python scripts/check_gates.py
python scripts/export_logs.py
```

### Validation gates

`scripts/check_gates.py` calcula o snapshot de qualidade das paper trades e
grava:

- `data/gates_check.json` — último estado para o dashboard
- tabela `gate_checks` — histórico em SQLite

Estados:
- `go` — amostra suficiente e todos os gates passam
- `no_go` — amostra suficiente e pelo menos um gate falha
- `partial` — ainda não há trades fechados suficientes

Thresholds configuráveis por env:
- `GATE_MIN_TRADES` (default 50)
- `GATE_MIN_PROFIT_FACTOR` (default 1.3)
- `GATE_MIN_AVG_R` (default 0.2)
- `GATE_MIN_WIN_RATE` (default 38)
- `GATE_MAX_STREAK_LOSSES` (default 5)
- `GATE_MAX_DRAWDOWN_PCT` (default 15)

Para ver histórico:

```bash
python scripts/check_gates.py --history 10
```

## Como ler o modal "Detalhes" do dashboard

Cada linha da tabela tem um botão **Detalhes**. Ao clicar, abre um modal
com cinco secções:

1. **Decisão IA** — sinal, score, confidence, razão e modelo usado.
2. **Features snapshot** — close, ATR, RSI, EMA20/EMA50, MACD,
   volatilidade, candles recentes (5 últimas).
3. **Análise técnica** — sinal estrito, RSI/EMA/MACD com valores e voto,
   razão técnica.
4. **Análise shadow** — sinal shadow, confidence, razão; e shadow
   combinado (IA + shadow).
5. **Decisão final** — sinal combinado estrito, sinal por score, motivo
   combinado, motivo de bloqueio, ATR/preço, timestamp e evento próximo.
6. (Se existir) **Paper trade associada** — direção, SL/TP, status,
   resultado em pips e R-multiple.

A tabela em si fica leve (sinais, score, status, ATR) — toda a
explicação detalhada vive no modal para não poluir.

## Testes

A pipeline de scoring, paper-trade e helpers críticos está coberta por
testes em `tests/`. Correr antes de qualquer alteração:

```bash
venv/bin/python -m pytest -v
```

O suite cobre:
- `tests/test_scoring.py` — math de scores, thresholds, pesos, edge cases
- `tests/test_database.py` — schema migrations, save_decision,
  paper trades CRUD, sumários filtrados (in-memory SQLite)
- `tests/test_paper_trade_evaluator.py` — TP / SL / expiry detection,
  R-multiple, integração end-to-end com DB temporária
- `tests/test_main_helpers.py` — `_build_paper_trade`,
  `_build_features_snapshot`, `_select_gating_signal`, `_volatility_label`,
  reasons construction
- `tests/test_ai_analyst.py` — formatação do prompt com snapshot técnico,
  fallback quando provider/key falham

São >120 testes, correm em <4s e usam apenas DB in-memory (não tocam em
`data/forex_bot.db`). O `conftest.py` limpa env vars relevantes para que
os testes não herdem o `.env` real.

## Backwards compatibility

Decisões antigas em SQLite não têm os novos campos. O código trata-as com
defaults seguros:
- `ai_score = null`, `combined_score = null`
- `ai_reason = ""`
- `ai_features_snapshot = {}`
- `ai_model_version = "unknown"`

O frontend renderiza `—` para qualquer valor null/undefined.
