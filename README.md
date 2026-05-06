<<<<<<< HEAD
# forex-ai-bot
Testing IA bot

## Validação rápida

```bash
venv/bin/python -m pytest -q
venv/bin/python scripts/evaluate_paper_trades.py
venv/bin/python scripts/check_gates.py --history 10
venv/bin/python scripts/export_logs.py
```

Cron recomendado para manter paper trades, gates e dashboard atualizados:

```cron
5 * * * 1-5 cd /root/forex-ai-bot && /root/forex-ai-bot/venv/bin/python main.py && /root/forex-ai-bot/venv/bin/python scripts/evaluate_paper_trades.py && /root/forex-ai-bot/venv/bin/python scripts/check_gates.py --quiet && /root/forex-ai-bot/venv/bin/python scripts/export_logs.py >> logs/cron.log 2>&1
```
=======

>>>>>>> 2ffb2d5bd9d46b6405d4c9feb39b31024b111f6d
