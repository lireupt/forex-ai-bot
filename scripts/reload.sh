#!/usr/bin/env bash
# Recarrega o sistema sem apagar dados.
#
# O que faz:
#   1. Para o monitor_trades.py (se estiver a correr)
#   2. git pull (pode ser ignorado com SKIP_GIT_PULL=1)
#   3. Migra colunas novas na DB (init_db via Python)
#   4. Avalia paper-trades abertas com candles existentes
#   5. Valida gates e exporta o dashboard
#   6. Copia web/ para o nginx web root
#   7. Recarrega nginx
#   8. Arranca o monitor_trades.py em background
#
# O que NÃO faz:
#   - Não apaga nem arquiva a base de dados
#   - Não apaga logs
#   - Não apaga web/data.json antes de regenerar
#
# Uso:
#   bash scripts/reload.sh
#   SKIP_GIT_PULL=1 bash scripts/reload.sh   # sem git pull
#   DEPLOY_WEB=0    bash scripts/reload.sh   # sem deploy nginx

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

PYTHON_BIN="${PYTHON_BIN:-$PROJECT_DIR/venv/bin/python}"
WEB_ROOT="${WEB_ROOT:-/var/www/forex-bot}"
SKIP_GIT_PULL="${SKIP_GIT_PULL:-0}"
DEPLOY_WEB="${DEPLOY_WEB:-1}"
RELOAD_NGINX="${RELOAD_NGINX:-1}"
MONITOR_LOG="$PROJECT_DIR/logs/monitor.log"

log()      { printf '[reload] %s\n' "$*"; }
ok()       { printf '[reload] ✓ %s\n' "$*"; }
warn()     { printf '[reload] ⚠ %s\n' "$*"; }
run_safe() {
  local desc="$1"; shift
  if "$@"; then ok "$desc"
  else warn "$desc falhou — continuar"
  fi
}

cd "$PROJECT_DIR"
mkdir -p logs web

# ── 1. Parar monitor ──────────────────────────────────────────────────────────
MONITOR_PID=$(pgrep -f "python.*monitor_trades.py" 2>/dev/null || true)
if [[ -n "$MONITOR_PID" ]]; then
  kill "$MONITOR_PID" 2>/dev/null && ok "monitor parado (PID $MONITOR_PID)"
  sleep 1
else
  log "monitor não estava a correr"
fi

# ── 2. Atualizar código ───────────────────────────────────────────────────────
if [[ "$SKIP_GIT_PULL" != "1" ]]; then
  run_safe "git pull" git pull
else
  log "git pull ignorado (SKIP_GIT_PULL=1)"
fi

# ── 3. Verificar Python ───────────────────────────────────────────────────────
if [[ ! -x "$PYTHON_BIN" ]]; then
  warn "Python não encontrado: $PYTHON_BIN"
  exit 1
fi

# ── 4. Migrar DB (adiciona colunas novas sem apagar dados) ────────────────────
run_safe "migração DB" "$PYTHON_BIN" - "$PROJECT_DIR" <<'PYEOF'
import sys, os
sys.path.insert(0, sys.argv[1])
os.chdir(sys.argv[1])
from dotenv import load_dotenv; load_dotenv(os.path.join(sys.argv[1], ".env"))
from modules import database
conn = database.connect()
database.init_db(conn)
conn.close()
print("  colunas verificadas/migradas")
PYEOF

# ── 5. Avaliar paper-trades abertas (candles existentes) ─────────────────────
run_safe "evaluate_paper_trades" "$PYTHON_BIN" scripts/evaluate_paper_trades.py

# ── 6. Validar gates e exportar dashboard ────────────────────────────────────
run_safe "check_gates"  "$PYTHON_BIN" scripts/check_gates.py --quiet
run_safe "export_logs"  "$PYTHON_BIN" scripts/export_logs.py

# ── 7. Deploy web ─────────────────────────────────────────────────────────────
if [[ "$DEPLOY_WEB" == "1" ]]; then
  if [[ -d "$WEB_ROOT" && ( -w "$WEB_ROOT" || "$(id -u)" -eq 0 ) ]]; then
    cp -rf web/. "$WEB_ROOT"/
    ok "dashboard copiado para $WEB_ROOT"
  else
    warn "deploy web ignorado: $WEB_ROOT não existe ou sem permissões"
  fi
fi

# ── 8. Recarregar nginx ───────────────────────────────────────────────────────
if [[ "$RELOAD_NGINX" == "1" ]]; then
  if command -v systemctl >/dev/null 2>&1; then
    if [[ "$(id -u)" -eq 0 ]]; then
      run_safe "nginx reload" systemctl reload nginx
    elif command -v sudo >/dev/null 2>&1; then
      run_safe "nginx reload" sudo systemctl reload nginx
    else
      warn "nginx reload ignorado: sem sudo"
    fi
  else
    warn "nginx reload ignorado: systemctl indisponível"
  fi
fi

# ── 9. Arrancar monitor em background ────────────────────────────────────────
nohup "$PYTHON_BIN" -u "$PROJECT_DIR/monitor_trades.py" >> "$MONITOR_LOG" 2>&1 &
NEW_PID=$!
sleep 2
if kill -0 "$NEW_PID" 2>/dev/null; then
  ok "monitor arrancado (PID $NEW_PID)"
else
  warn "monitor não arrancou — verificar $MONITOR_LOG"
fi

# ── Sumário ───────────────────────────────────────────────────────────────────
echo ""
log "─────────────────────────────────────────"
log "Reload concluído. Base de dados intacta."
log "─────────────────────────────────────────"
ls -lh data/forex_bot.db 2>/dev/null && true
ls -lh web/data.json     2>/dev/null && true
[[ "$DEPLOY_WEB" == "1" && -f "$WEB_ROOT/data.json" ]] && ls -lh "$WEB_ROOT/data.json" || true
echo ""
log "Monitor: tail -f logs/monitor.log"
log "Ciclo:   tail -f logs/cron.log"
