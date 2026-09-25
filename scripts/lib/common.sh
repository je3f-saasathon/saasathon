#!/usr/bin/env bash
# Shared helpers for scripts/*.sh. Bash 3.2 (macOS) and modern bash compatible:
# no associative arrays, no mapfile, no ${var,,}, no wait -n.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUN_DIR="$REPO_ROOT/.run"
LOG_DIR="$RUN_DIR/logs"

# ---- colour output ----------------------------------------------------
if [ -t 1 ]; then
  C_RED=$'\033[31m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'
  C_BLUE=$'\033[34m'; C_BOLD=$'\033[1m'; C_RESET=$'\033[0m'
else
  C_RED=""; C_GREEN=""; C_YELLOW=""; C_BLUE=""; C_BOLD=""; C_RESET=""
fi

log_info()  { printf '%s[info]%s %s\n'  "$C_BLUE"  "$C_RESET" "$*"; }
log_ok()    { printf '%s[ ok ]%s %s\n'  "$C_GREEN" "$C_RESET" "$*"; }
log_warn()  { printf '%s[warn]%s %s\n'  "$C_YELLOW" "$C_RESET" "$*"; }
log_err()   { printf '%s[fail]%s %s\n'  "$C_RED"   "$C_RESET" "$*" >&2; }

# ---- OS / arch detection -----------------------------------------------
os_name() { uname -s; }               # Darwin | Linux
is_mac()   { [ "$(uname -s)" = "Darwin" ]; }
is_linux() { [ "$(uname -s)" = "Linux" ]; }

# ---- env file loading ---------------------------------------------------
# Usage: env_get <path-to-env-file> <KEY> <default>
env_get() {
  file="$1"; key="$2"; default="$3"
  if [ -f "$file" ]; then
    val=$(grep -E "^${key}=" "$file" 2>/dev/null | tail -n1 | cut -d'=' -f2- | sed -e 's/^"//' -e 's/"$//')
    if [ -n "$val" ]; then echo "$val"; return; fi
  fi
  echo "$default"
}

ensure_env_file() {
  # Usage: ensure_env_file <root-dir>
  root="$1"
  if [ ! -f "$root/.env" ] && [ -f "$root/.env.example" ]; then
    cp "$root/.env.example" "$root/.env"
    log_ok "created $root/.env from .env.example"
  fi
}

frontend_port() { env_get "$REPO_ROOT/frontend/.env" FRONTEND_PORT 5173; }
backend_port()  { env_get "$REPO_ROOT/backend/.env" BACKEND_PORT 8000; }
db_port()       { echo 5432; }

mkdir -p "$RUN_DIR" "$LOG_DIR" 2>/dev/null || true

# ---- portable process-group launch (setsid doesn't exist on macOS) -----
# Usage: start_bg <name> <logfile> <cmd...>
# Writes PID (of the new process group leader) to .run/<name>.pid
start_bg() {
  name="$1"; logfile="$2"; shift 2
  if is_mac; then
    python3 - "$RUN_DIR/${name}.pid" "$logfile" "$@" <<'PYEOF' &
import os, sys
pidfile, logfile = sys.argv[1], sys.argv[2]
cmd = sys.argv[3:]
os.setsid()
with open(pidfile, "w") as f:
    f.write(str(os.getpid()))
log = open(logfile, "a")
os.dup2(log.fileno(), 1)
os.dup2(log.fileno(), 2)
os.execvp(cmd[0], cmd)
PYEOF
    disown 2>/dev/null || true
  else
    setsid "$@" >>"$logfile" 2>&1 &
    echo $! > "$RUN_DIR/${name}.pid"
    disown 2>/dev/null || true
  fi
}

pid_alive() { kill -0 "$1" 2>/dev/null; }

# Kill a tracked process group given a name (reads .run/<name>.pid)
stop_tracked() {
  name="$1"
  pidfile="$RUN_DIR/${name}.pid"
  [ -f "$pidfile" ] || return 0
  pid=$(cat "$pidfile" 2>/dev/null)
  if [ -n "$pid" ] && pid_alive "$pid"; then
    log_info "stopping $name (pgid $pid)"
    kill -TERM "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    i=0
    while [ $i -lt 50 ] && pid_alive "$pid"; do sleep 0.1; i=$((i+1)); done
    if pid_alive "$pid"; then
      kill -KILL "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
    fi
  fi
  rm -f "$pidfile"
}

# ---- port inspection -----------------------------------------------------
# Prints "pid|command" of the listener on a TCP port, or nothing.
port_listener() {
  port="$1"
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"$port" -sTCP:LISTEN 2>/dev/null | awk 'NR==2{print $2"|"$1}' || true
  elif command -v ss >/dev/null 2>&1; then
    ss -ltnp 2>/dev/null | grep ":$port " | sed -n 's/.*pid=\([0-9]*\).*/\1|proc/p' | head -n1 || true
  elif command -v fuser >/dev/null 2>&1; then
    p=$(fuser -n tcp "$port" 2>/dev/null | tr -d ' ' || true)
    [ -n "$p" ] && echo "$p|unknown"
  fi
}

looks_like_ours() {
  # Usage: looks_like_ours <pid>
  pid="$1"
  cmdline=$(ps -o command= -p "$pid" 2>/dev/null || true)
  case "$cmdline" in
    *uvicorn*|*gunicorn*|*"manage.py runserver"*|*vite*|*"node "*"vite"*)
      case "$cmdline" in
        *"$REPO_ROOT"*) return 0 ;;
      esac
      ;;
  esac
  return 1
}

# Usage: ensure_port_free <port> <force:0|1>
# Returns 0 if the port is free (or was freed). Prints guidance otherwise.
ensure_port_free() {
  port="$1"; force="${2:-0}"
  info=$(port_listener "$port")
  [ -z "$info" ] && return 0
  pid=${info%%|*}
  cmd=${info##*|}
  # is it one of our tracked processes?
  for pidfile in "$RUN_DIR"/*.pid; do
    [ -f "$pidfile" ] || continue
    tpid=$(cat "$pidfile" 2>/dev/null)
    if [ "$tpid" = "$pid" ]; then
      name=$(basename "$pidfile" .pid)
      stop_tracked "$name"
      return 0
    fi
  done
  if looks_like_ours "$pid"; then
    log_warn "port $port held by an untracked process that looks like ours (pid $pid, $cmd) — stopping it"
    kill -TERM "$pid" 2>/dev/null || true
    sleep 0.3
    kill -KILL "$pid" 2>/dev/null || true
    return 0
  fi
  if [ "$force" = "1" ]; then
    log_warn "port $port held by pid $pid ($cmd) — force-stopping"
    kill -TERM "$pid" 2>/dev/null || true
    sleep 0.3
    kill -KILL "$pid" 2>/dev/null || true
    return 0
  fi
  log_err "port $port is in use by an unrelated process: pid $pid ($cmd)"
  log_err "  fix: kill $pid    (or re-run with --force)"
  return 1
}

wait_for_http() {
  # Usage: wait_for_http <url> <timeout-seconds>
  url="$1"; timeout="${2:-30}"
  i=0
  while [ "$i" -lt "$timeout" ]; do
    if command -v curl >/dev/null 2>&1; then
      if curl -fsS -o /dev/null "$url" 2>/dev/null; then return 0; fi
    fi
    sleep 1
    i=$((i+1))
  done
  return 1
}

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    log_err "missing required command: $1"
    [ -n "${2:-}" ] && log_err "  install: $2"
    return 1
  fi
}
