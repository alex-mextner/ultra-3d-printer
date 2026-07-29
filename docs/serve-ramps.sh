#!/usr/bin/env bash
# Универсальный статический сервер для docs/ (диагностический HTML-гайд и его ассеты).
# Использует python3/python -m http.server — работает одинаково на Linux/Mac/Windows(Git Bash).
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${PORT:-8000}"
LOG="$ROOT/serve-ramps.log"

log() {
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" >>"$LOG"
}

PY=""
if command -v python3 >/dev/null 2>&1; then
    PY=python3
elif command -v python >/dev/null 2>&1; then
    PY=python
fi

if [ -z "$PY" ]; then
    echo "Нужен python3 или python в PATH — ни то ни другое не найдено." >&2
    exit 1
fi

log "=== server script starting, pid=$$ ==="
log "using $PY -m http.server on port $PORT, root=$ROOT"
cd "$ROOT" || exit 1
while true; do
    "$PY" -m http.server "$PORT" >>"$LOG" 2>&1
    log "http.server exited - restarting in 2s"
    sleep 2
done
