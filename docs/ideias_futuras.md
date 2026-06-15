# Ideias Futuras

Notas de investigação para versões futuras do forex-ai-bot. Não comprometidas para implementação imediata.

---

## Ideia 1 — Camada 4: Debate Bull/Bear entre agentes LLM especializados

**Inspiração:** TradingAgents (Xiao et al., 2024)

Em vez de um único agente contextual, criar dois agentes LLM adversários:
- **Agente Bull** — procura argumentos que justifiquem uma entrada LONG
- **Agente Bear** — procura argumentos que justifiquem uma entrada SHORT

Os dois debatem com base nas mesmas notícias e dados técnicos. Um terceiro agente árbitro lê o debate e emite o `bias` final, o `confidence_adjustment` e o `news_sentiment`.

**Vantagem esperada:** redução de viés de confirmação. O agente actual tende a reforçar o sinal técnico em vez de o desafiar.

**Implementação sugerida:**
- Criar `modules/ai_debate.py` com `run_debate(news, events, technical) -> ai_result`
- Substituir `ai_analyst.analyse()` em `main.py` quando `AI_DEBATE_MODE=true`
- Custo: 3 chamadas LLM por ciclo (vs. 1 actual)

---

## Ideia 2 — Camada 5: Reflexões ancoradas em resultados (Outcome-Grounded)

Após fechar um paper-trade (win/loss/timeout), alimentar o resultado de volta à IA como contexto na próxima análise do mesmo par:

```
"Na última decisão similar (BUY, adj=+0.20, news_sentiment=positive),
 o trade perdeu 12 pips em 4h. Re-avaliar o peso dado ao sentimento positivo
 quando o RSI já estava acima de 60."
```

**Objectivo:** aprendizagem contextual sem fine-tuning. O bot passa a ter memória de curto prazo sobre os seus próprios erros recentes.

**Implementação sugerida:**
- `database.get_last_outcome_for_pair(conn, pair, n=3)` → últimos N resultados fechados
- Formatar como bloco de texto no prompt do `ai_analyst`
- Guardar em `modules/outcome_context.py`
- Habilitar com `OUTCOME_CONTEXT_ENABLED=true`

---

## Referência Académica

> Xiao, Y., Sun, E., Luo, D., & Wang, W. (2024).
> **TradingAgents: Multi-Agents LLM Financial Trading Framework.**
> *arXiv:2412.20138.* https://arxiv.org/abs/2412.20138
