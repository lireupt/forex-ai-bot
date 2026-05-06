# Deploy do forex-ai-bot em Ubuntu VPS

Este documento descreve os passos para instalar e operar o `forex-ai-bot` num servidor Ubuntu limpo.

Contexto usado:

- Projeto: `forex-ai-bot`
- Sistema operativo: Ubuntu 24.04
- Runtime: Python `venv`
- Scheduler: `cron`
- Base de dados: SQLite
- Dashboard web: HTML estático servido por Nginx
- Comando do bot: `venv/bin/python main.py`
- Export do dashboard: `scripts/export_logs.py`
- Caminho do projeto no servidor: `/root/forex-ai-bot`
- Web root: `/var/www/forex-bot`
- Acesso público por IP, sem domínio por agora
- Basic Auth opcional pode ser adicionado mais tarde

## 1. Requisitos

- Ubuntu 24.04
- Acesso SSH como `root` ou utilizador com `sudo`
- Git instalado
- Python 3, `pip` e `venv`
- Nginx
- SQLite CLI opcional
- Chave API Groq

## 2. Atualizar servidor

```bash
apt update && apt upgrade -y
reboot
```

O `reboot` pode ser necessário depois de atualização de kernel. Se a sessão SSH cair, voltar a entrar no servidor após alguns segundos.

## 3. Instalar dependências do sistema

```bash
apt install python3 python3-pip python3-venv git nginx sqlite3 -y
```

## 4. Clonar projeto

```bash
cd /root
git clone https://github.com/lireupt/forex-ai-bot.git
cd forex-ai-bot
```

Se o repositório for privado, usar uma chave SSH ou um GitHub Personal Access Token. Login com password normal no GitHub já não funciona.

## 5. Criar ambiente Python

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Se o pacote Groq estiver em falta:

```bash
pip install groq python-dotenv
pip freeze > requirements.txt
```

## 6. Criar ficheiro `.env`

```bash
nano .env
```

Exemplo:

```env
DRY_RUN=True
USE_CACHE=True
FORCE_REFRESH=False
AI_PROVIDER=groq
GROQ_API_KEY=YOUR_KEY_HERE
GROQ_MODEL=llama-3.1-8b-instant
AI_CACHE_DAILY=True
PRICE_CACHE_MINUTES=60
DEDUP_WINDOW_MINUTES=50
BLOCK_NEAR_HIGH_IMPACT_EVENTS=True
EVENT_BLOCK_WINDOW_MINUTES=120
USE_ATR_SL_TP=True
ATR_SL_MULT=1.5
ATR_TP_MULT=3.0
ATR_MIN_SL_PIPS=12
ATR_MAX_SL_PIPS=60
```

Notas de segurança:

- Nunca fazer commit do `.env`.
- Nunca expor API keys em logs, screenshots, issues ou dashboard público.
- Confirmar que `.env` está no `.gitignore`.

## 7. Testar ligação à Groq API

Executar dentro de `/root/forex-ai-bot`:

```bash
python3 - <<'PY'
import os
from dotenv import load_dotenv
from groq import Groq

load_dotenv(dotenv_path=".env")

key = os.getenv("GROQ_API_KEY")
if not key:
    raise SystemExit("GROQ_API_KEY não encontrada")

client = Groq(api_key=key)

resp = client.chat.completions.create(
    model=os.getenv("GROQ_MODEL", "llama-3.1-8b-instant"),
    messages=[{"role": "user", "content": "Responde apenas: OK"}],
    temperature=0,
    max_tokens=10,
)

print(resp.choices[0].message.content)
PY
```

Resultado esperado:

```text
OK
```

## 8. Testar bot manualmente

```bash
venv/bin/python main.py
```

Comportamento esperado:

- Estado de notícias/cache aparece no terminal.
- Estado de calendário/cache aparece no terminal.
- Análise IA aparece.
- Análise técnica aparece.
- Decisão DRY RUN aparece.
- Logs JSONL e SQLite são guardados.

## 9. Consultar SQLite

```bash
sqlite3 data/forex_bot.db
```

Dentro da consola SQLite:

```sql
.tables

SELECT 'market_candles', COUNT(*) FROM market_candles;
SELECT 'news_items', COUNT(*) FROM news_items;
SELECT 'economic_events', COUNT(*) FROM economic_events;
SELECT 'ai_analyses', COUNT(*) FROM ai_analyses;
SELECT 'decisions', COUNT(*) FROM decisions;

SELECT timestamp, pair, ai_signal, technical_signal, combined_signal, confidence, trade_allowed, block_reason
FROM decisions
ORDER BY timestamp DESC
LIMIT 10;

.quit
```

## 10. Configurar dashboard estático

Gerar export:

```bash
venv/bin/python scripts/export_logs.py
```

Confirmar ficheiros:

```bash
ls web/
```

Esperado:

- `index.html`
- `data.json`

## 11. Configurar Nginx

```bash
mkdir -p /var/www/forex-bot
cp -r /root/forex-ai-bot/web/* /var/www/forex-bot/
```

Criar configuração:

```bash
nano /etc/nginx/sites-available/forex-bot
```

Config:

```nginx
server {
    listen 80;
    server_name _;

    root /var/www/forex-bot;
    index index.html;

    location / {
        try_files $uri $uri/ =404;
    }
}
```

Ativar site:

```bash
ln -s /etc/nginx/sites-available/forex-bot /etc/nginx/sites-enabled/
rm /etc/nginx/sites-enabled/default
nginx -t
systemctl restart nginx
```

Acesso:

```text
http://SERVER_IP
```

## 12. Configurar cron

```bash
crontab -e
```

Cron hourly:

```cron
5 * * * * cd /root/forex-ai-bot && /root/forex-ai-bot/venv/bin/python main.py >> logs/cron.log 2>&1
```

Isto corre o bot todas as horas ao minuto 5, 24h/dia.

Se for desejado apenas segunda a sexta:

```cron
5 * * * 1-5 cd /root/forex-ai-bot && /root/forex-ai-bot/venv/bin/python main.py >> logs/cron.log 2>&1
```

Se for desejado apenas durante horas de trading ativas:

```cron
5 7-21 * * 1-5 cd /root/forex-ai-bot && /root/forex-ai-bot/venv/bin/python main.py >> logs/cron.log 2>&1
```

Cron completo com avaliação de paper trades, validation gates e export do
dashboard:

```cron
5 * * * 1-5 cd /root/forex-ai-bot && /root/forex-ai-bot/venv/bin/python main.py && /root/forex-ai-bot/venv/bin/python scripts/evaluate_paper_trades.py && /root/forex-ai-bot/venv/bin/python scripts/check_gates.py --quiet && /root/forex-ai-bot/venv/bin/python scripts/export_logs.py >> logs/cron.log 2>&1
```

## 13. Confirmar cron

```bash
crontab -l
tail -f logs/cron.log
wc -l logs/decisions.jsonl
```

Notas:

- `CTRL+C` sai do `tail -f`.
- `decisions.jsonl` deve crescer ao longo do tempo.
- Se `cron.log` não aparecer imediatamente, aguardar até ao minuto configurado no cron.

## 14. Firewall básico

```bash
ufw allow OpenSSH
ufw allow 'Nginx Full'
ufw enable
ufw status
```

Confirmar que SSH continua permitido antes de fechar a sessão atual.

## 15. Notas de segurança

- O servidor fica público se o dashboard estiver acessível por IP.
- O dashboard deve idealmente ser protegido mais tarde com Nginx Basic Auth.
- Não expor logs brutos, `.env`, base de dados SQLite ou API keys.
- Manter `DRY_RUN=True` até a validação estar completa.
- Ainda não existe OANDA/live trading real.
- Não guardar chaves privadas dentro do repositório.

## 16. Troubleshooting

### A) Página default do Nginx aparece

Corrigir:

```bash
rm /etc/nginx/sites-enabled/default
systemctl restart nginx
```

### B) GitHub pede password

Motivo:

GitHub já não aceita password normal para `git clone` ou `git pull`.

Usar:

- chave SSH; ou
- Personal Access Token.

### C) `ModuleNotFoundError: groq`

Corrigir:

```bash
source venv/bin/activate
pip install groq python-dotenv
pip freeze > requirements.txt
```

### D) `load_dotenv AssertionError` em teste via stdin

Corrigir usando caminho explícito:

```python
load_dotenv(dotenv_path=".env")
```

### E) Mensagem de kernel upgrade

Corrigir:

```bash
reboot
```

### F) `tail -f` parece bloqueado

Isto é normal. O `tail -f` fica a seguir o ficheiro de log em tempo real.

Sair com:

```text
CTRL+C
```

## 17. Modo operacional atual

- O bot corre em `DRY_RUN`.
- O cron corre automaticamente.
- As decisões são guardadas em JSONL e SQLite.
- O dashboard lê dados estáticos exportados.
- O sinal strict continua conservador.
- O shadow signal é apenas para comparação.
- Outcomes/backfill precisam de candles futuros antes de aparecerem.

## 18. Checklist final

- [ ] Server updated
- [ ] Project cloned
- [ ] venv created
- [ ] requirements installed
- [ ] `.env` configured
- [ ] Groq API tested
- [ ] `main.py` runs manually
- [ ] SQLite created
- [ ] `export_logs.py` works
- [ ] Nginx serves dashboard
- [ ] cron active
- [ ] `cron.log` checked
- [ ] firewall enabled
- [ ] `DRY_RUN` confirmed
