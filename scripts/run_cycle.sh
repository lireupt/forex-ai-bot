#!/usr/bin/env bash
# Ciclo recorrente do bot (para cron). Ao contrário do reset_and_bootstrap.sh,
# NÃO arquiva a DB: apenas corre o ciclo, avalia paper trades, valida gates,
# exporta o data.json e implanta-o no web root que o nginx serve.
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

PYTHON_BIN="${PYTHON_BIN:-$PROJECT_DIR/venv/bin/python}"
WEB_ROOT="${WEB_ROOT:-/var/www/forex-bot}"
BOT_MODE_VALUE="${1:-${BOT_MODE:-trade}}"
DEPLOY_WEB="${DEPLOY_WEB:-1}"

log() {
  printf '[cycle] %s\n' "$*"
}

cd "$PROJECT_DIR"

if [[ ! -x "$PYTHON_BIN" ]]; then
  log "Python não encontrado/executável: $PYTHON_BIN"
  exit 1
fi

log "a iniciar ciclo em BOT_MODE=$BOT_MODE_VALUE"
BOT_MODE="$BOT_MODE_VALUE" "$PYTHON_BIN" main.py
"$PYTHON_BIN" scripts/evaluate_paper_trades.py
"$PYTHON_BIN" scripts/check_gates.py --quiet
"$PYTHON_BIN" scripts/export_logs.py

# Deploy: o nginx serve de $WEB_ROOT, mas o export escreve em web/data.json.
# Sem este passo o dashboard fica congelado no último deploy manual.
if [[ "$DEPLOY_WEB" == "1" ]]; then
  if [[ -d "$WEB_ROOT" && ( -w "$WEB_ROOT" || "$(id -u)" -eq 0 ) ]]; then
    cp -f web/data.json "$WEB_ROOT"/data.json
    log "data.json implantado em $WEB_ROOT"
  else
    log "deploy web ignorado: $WEB_ROOT não existe ou sem permissões"
  fi
fi

log "ciclo concluído"
