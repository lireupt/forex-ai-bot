#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

PYTHON_BIN="${PYTHON_BIN:-$PROJECT_DIR/venv/bin/python}"
WEB_ROOT="${WEB_ROOT:-/var/www/forex-bot}"
BOT_MODE_VALUE="${1:-${BOT_MODE:-trade}}"

SKIP_GIT_PULL="${SKIP_GIT_PULL:-0}"
DEPLOY_WEB="${DEPLOY_WEB:-1}"
RELOAD_NGINX="${RELOAD_NGINX:-1}"
RESET_LOG_FILES="${RESET_LOG_FILES:-1}"

log() {
  printf '[reset] %s\n' "$*"
}

run_or_warn() {
  local description="$1"
  shift
  if "$@"; then
    log "$description: OK"
  else
    log "$description: falhou, continuar"
  fi
}

cd "$PROJECT_DIR"

mkdir -p data/archive logs web

if [[ -f data/forex_bot.db ]]; then
  archive_path="data/archive/forex_bot_reset_$(date -u +%Y%m%d_%H%M%S).db"
  mv data/forex_bot.db "$archive_path"
  log "DB arquivada em $archive_path"
else
  log "sem data/forex_bot.db para arquivar"
fi

rm -f data/gates_check.json
rm -f web/data.json

if [[ "$RESET_LOG_FILES" == "1" ]]; then
  shopt -s nullglob
  rm -f logs/*.log
  shopt -u nullglob
  log "logs/*.log removidos"
fi

if [[ "$DEPLOY_WEB" == "1" && -f "$WEB_ROOT/data.json" ]]; then
  rm -f "$WEB_ROOT/data.json"
  log "$WEB_ROOT/data.json removido"
fi

if [[ "$SKIP_GIT_PULL" != "1" ]]; then
  git pull
fi

if [[ ! -x "$PYTHON_BIN" ]]; then
  log "Python não encontrado/executável: $PYTHON_BIN"
  exit 1
fi

log "a iniciar ciclo em BOT_MODE=$BOT_MODE_VALUE"
BOT_MODE="$BOT_MODE_VALUE" "$PYTHON_BIN" main.py
"$PYTHON_BIN" scripts/evaluate_paper_trades.py
"$PYTHON_BIN" scripts/check_gates.py --quiet
"$PYTHON_BIN" scripts/export_logs.py

if [[ "$DEPLOY_WEB" == "1" ]]; then
  if [[ -d "$WEB_ROOT" && -w "$WEB_ROOT" ]]; then
    cp -r web/. "$WEB_ROOT"/
    log "dashboard copiado para $WEB_ROOT"
  elif [[ -d "$WEB_ROOT" && "$(id -u)" -eq 0 ]]; then
    cp -r web/. "$WEB_ROOT"/
    log "dashboard copiado para $WEB_ROOT"
  else
    log "deploy web ignorado: $WEB_ROOT não existe ou não tem permissões"
  fi
fi

if [[ "$RELOAD_NGINX" == "1" ]]; then
  if command -v systemctl >/dev/null 2>&1; then
    if [[ "$(id -u)" -eq 0 ]]; then
      run_or_warn "nginx reload" systemctl reload nginx
    elif command -v sudo >/dev/null 2>&1; then
      run_or_warn "nginx reload" sudo systemctl reload nginx
    else
      log "nginx reload ignorado: sudo indisponível"
    fi
  else
    log "nginx reload ignorado: systemctl indisponível"
  fi
fi

ls -lh data/forex_bot.db web/data.json 2>/dev/null || true
if [[ "$DEPLOY_WEB" == "1" && -f "$WEB_ROOT/data.json" ]]; then
  ls -lh "$WEB_ROOT/data.json"
fi

log "reset concluído"
