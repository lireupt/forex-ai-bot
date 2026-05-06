# Dashboard estático

Página simples (HTML + CSS + JS sem build) que mostra as últimas decisões do
bot a partir de `data.json`. Sem backend Python — Nginx serve só ficheiros
estáticos.

## Como funciona

1. `main.py` chama `scripts/export_logs.py` no fim de cada execução.
2. `scripts/check_gates.py` escreve o snapshot de validação em
   `data/gates_check.json` e guarda histórico na tabela `gate_checks`.
3. `scripts/export_logs.py` lê o SQLite (`data/forex_bot.db`) — fallback para
   `logs/decisions.jsonl` — e escreve as últimas 50 decisões, paper trades,
   summary e gates para `web/data.json`.
4. `index.html` faz `fetch("data.json")` a cada 60 segundos.

## Exportar manualmente

```bash
venv/bin/python scripts/export_logs.py
```

Com argumentos:

```bash
venv/bin/python scripts/export_logs.py --limit 100 --out web/data.json
```

Para atualizar também os gates antes do export:

```bash
venv/bin/python scripts/evaluate_paper_trades.py
venv/bin/python scripts/check_gates.py --quiet
venv/bin/python scripts/export_logs.py
```

## Testar localmente

A partir da raiz do projecto:

```bash
cd web && python3 -m http.server 8080
```

Abre `http://localhost:8080` no browser. O auto-refresh dispara de minuto a
minuto, mas como aqui só há export quando corres o bot ou o script, vais ver
sempre os mesmos dados até nova execução.

## Deploy num VPS com Nginx

1. Instala o Nginx:
   ```bash
   sudo apt update && sudo apt install nginx
   ```
2. Copia o exemplo de config:
   ```bash
   sudo cp nginx/forex-bot.conf.example /etc/nginx/sites-available/forex-bot
   sudo ln -s /etc/nginx/sites-available/forex-bot /etc/nginx/sites-enabled/forex-bot
   sudo rm -f /etc/nginx/sites-enabled/default
   ```
3. Ajusta `server_name` (e o `root` se preferires outra pasta), depois:
   ```bash
   sudo nginx -t && sudo systemctl reload nginx
   ```
4. Coloca a pasta `web/` no caminho indicado pelo `root` do config. Há duas
   estratégias:

   - **Symlink** (mantém os ficheiros versionados na pasta do projecto):
     ```bash
     sudo ln -s /home/<user>/forex-ai-bot/web /var/www/forex-bot
     ```
   - **Copy** (mais isolado, precisa rsync após cada export):
     ```bash
     sudo rsync -a --delete /home/<user>/forex-ai-bot/web/ /var/www/forex-bot/
     ```

   Se usares symlink, garante que `/home/<user>` é acessível ao utilizador
   `www-data` (`chmod o+x` no path da home).

5. Confirma com `curl http://localhost/` e `curl http://localhost/data.json`.

## Proteger com Basic Auth (recomendado)

Se o dashboard for exposto na internet, descomenta o bloco `auth_basic`
no config de exemplo e cria o ficheiro de credenciais:

```bash
sudo apt install apache2-utils
sudo htpasswd -c /etc/nginx/.forex-bot.htpasswd <utilizador>
sudo systemctl reload nginx
```

Para HTTPS, instala o certbot e corre:

```bash
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d <dominio>
```

## O que NÃO é exportado

`scripts/export_logs.py` aplica whitelist de campos. Não vai parar ao
`data.json`:

- API keys / `.env`
- Texto cru das notícias e das respostas da IA
- Linhas individuais de candles
- Eventos económicos detalhados
- Entradas duplicadas com a mesma assinatura

Se quiseres expor mais campos, edita `EXPORT_FIELDS` e a função `_normalise`
em `scripts/export_logs.py`.

## Cron completo

```cron
5 * * * 1-5 cd /root/forex-ai-bot && /root/forex-ai-bot/venv/bin/python main.py && /root/forex-ai-bot/venv/bin/python scripts/evaluate_paper_trades.py && /root/forex-ai-bot/venv/bin/python scripts/check_gates.py --quiet && /root/forex-ai-bot/venv/bin/python scripts/export_logs.py >> logs/cron.log 2>&1
```
