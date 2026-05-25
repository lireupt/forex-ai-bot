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
