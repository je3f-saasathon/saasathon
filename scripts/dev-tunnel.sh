#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/lib/common.sh"

TUNNEL_ENV="$REPO_ROOT/scripts/tunnel.env"
TUNNEL_ENV_EXAMPLE="$REPO_ROOT/scripts/tunnel.env.example"
TUNNEL_PID_FILE="$RUN_DIR/tunnel.pid"

usage() {
  cat <<'EOF'
Usage: scripts/dev-tunnel.sh {up|down|kill|tunnel|status|logs} [svc] [--db] [--force]

Runs the dev stack on a remote host over SSH and forwards its ports back
to localhost, so all URLs stay http://localhost:<port>. Also forwards the
SRE agent's Langfuse and Temporal UIs when those local ports are free.

Commands (default: up):
  up       ensure the remote stack is running, then open the tunnel (foreground)
  down     stop the remote stack (ssh `make stop`), no tunnel
  kill     hard-stop the remote stack (`make stop ALL=1` + compose kill/down), no tunnel
  tunnel   just open the tunnel to whatever is already running remotely
  status   ssh `make status` on the remote
  logs     ssh `make logs SERVICE=<svc>`, streamed

Config comes from scripts/tunnel.env (gitignored, auto-created from
scripts/tunnel.env.example on first run). Real environment variables
override the file: REMOTE_HOST, REMOTE_PROJECT_DIR, REPO_URL, REMOTE_MODE.

  --db      also forward the database port (not forwarded by default)
  --force   allow closing an untracked local process on a needed port
  -h, --help
EOF
}

CMD="up"
SVC=""
FORWARD_DB=0
FORCE=0

# first non-flag arg is the command; second (for logs) is the service
args=("$@")
positional=()
for a in "${args[@]}"; do
  case "$a" in
    --db) FORWARD_DB=1 ;;
    --force) FORCE=1 ;;
    -h|--help) usage; exit 0 ;;
    *) positional+=("$a") ;;
  esac
done
if [ "${#positional[@]}" -ge 1 ]; then CMD="${positional[0]}"; fi
if [ "${#positional[@]}" -ge 2 ]; then SVC="${positional[1]}"; fi

case "$CMD" in
  up|down|kill|tunnel|status|logs) ;;
  *) log_err "unknown command: $CMD"; usage; exit 1 ;;
esac

# ---- load config ---------------------------------------------------------
if [ ! -f "$TUNNEL_ENV" ]; then
  cp "$TUNNEL_ENV_EXAMPLE" "$TUNNEL_ENV"
  log_err "scripts/tunnel.env did not exist — created it from tunnel.env.example"
  log_err "please edit scripts/tunnel.env (REMOTE_HOST etc) and re-run"
  exit 1
fi

REMOTE_HOST="${REMOTE_HOST:-$(env_get "$TUNNEL_ENV" REMOTE_HOST oma)}"
REMOTE_PROJECT_DIR="${REMOTE_PROJECT_DIR:-$(env_get "$TUNNEL_ENV" REMOTE_PROJECT_DIR '$HOME/saasathon')}"
REPO_URL="${REPO_URL:-$(env_get "$TUNNEL_ENV" REPO_URL https://github.com/je3f-saasathon/saasathon.git)}"
REMOTE_MODE="${REMOTE_MODE:-$(env_get "$TUNNEL_ENV" REMOTE_MODE docker)}"

ssh_run() {
  # Usage: ssh_run <remote-shell-command-string>
  ssh -o BatchMode=yes -o ConnectTimeout=10 "$REMOTE_HOST" "$1"
}

preflight() {
  if ! ssh -o BatchMode=yes -o ConnectTimeout=5 "$REMOTE_HOST" true >/dev/null 2>&1; then
    log_err "cannot reach '$REMOTE_HOST' over SSH"
    log_err "  fix: confirm a Host entry for '$REMOTE_HOST' exists in ~/.ssh/config,"
    log_err "       that key-based auth works, and that the host is reachable:"
    log_err "       ssh $REMOTE_HOST true"
    exit 1
  fi
}

remote_ensure_checkout() {
  if ssh_run "test -d $REMOTE_PROJECT_DIR/.git"; then
    dirty=$(ssh_run "cd $REMOTE_PROJECT_DIR && git status --porcelain" || true)
    branch=$(ssh_run "cd $REMOTE_PROJECT_DIR && git rev-parse --abbrev-ref HEAD" || true)
    if [ -n "$dirty" ]; then
      log_warn "remote checkout at $REMOTE_PROJECT_DIR has uncommitted changes — leaving it as-is"
    fi
    if [ -n "$branch" ] && [ "$branch" != "main" ]; then
      log_warn "remote checkout is on branch '$branch', not main — leaving it as-is"
    fi
    return 0
  fi

  if [ ! -t 0 ]; then
    log_err "remote project dir $REMOTE_PROJECT_DIR has no git checkout"
    log_err "  run this by hand, then re-run scripts/dev-tunnel.sh:"
    log_err "  ssh $REMOTE_HOST \"git clone $REPO_URL $REMOTE_PROJECT_DIR\""
    exit 1
  fi

  printf '%s' "remote project dir has no git checkout — clone $REPO_URL there now? [y/N] "
  read -r reply
  case "$reply" in
    y|Y|yes|YES) ssh_run "git clone $REPO_URL $REMOTE_PROJECT_DIR" ;;
    *) log_err "aborted — nothing cloned"; exit 1 ;;
  esac
}

remote_ensure_env() {
  need_setup=$(ssh_run "test -f $REMOTE_PROJECT_DIR/backend/.env -a -f $REMOTE_PROJECT_DIR/frontend/.env && echo no || echo yes")
  if [ "$need_setup" = "yes" ]; then
    log_info "remote .env files missing — running make setup on the remote"
    if [ "$REMOTE_MODE" = "docker" ]; then
      ssh_run "cd $REMOTE_PROJECT_DIR && make setup DOCKER_ONLY=1 YES=1"
    else
      ssh_run "cd $REMOTE_PROJECT_DIR && make setup YES=1"
    fi
  fi
}

remote_start_stack() {
  log_info "starting remote stack ($REMOTE_MODE mode, detached)"
  if [ "$REMOTE_MODE" = "docker" ]; then
    ssh_run "cd $REMOTE_PROJECT_DIR && make dev-docker DETACH=1"
  else
    ssh_run "cd $REMOTE_PROJECT_DIR && nohup make dev >/dev/null 2>&1 & disown; sleep 1; true"
  fi
}

remote_ports() {
  fe=$(ssh_run "grep -E '^FRONTEND_PORT=' $REMOTE_PROJECT_DIR/frontend/.env 2>/dev/null | tail -n1 | cut -d= -f2-" || true)
  be=$(ssh_run "grep -E '^BACKEND_PORT=' $REMOTE_PROJECT_DIR/backend/.env 2>/dev/null | tail -n1 | cut -d= -f2-" || true)
  echo "${fe:-5173} ${be:-8000}"
}

# SRE agent UIs on the remote (docker-compose.infra.yml): Langfuse's port comes
# from the remote LANGFUSE_HOST, the Temporal UI uses the infra default.
remote_sre_ports() {
  lf_url=$(ssh_run "grep -E '^LANGFUSE_HOST=' $REMOTE_PROJECT_DIR/backend/.env 2>/dev/null | tail -n1 | cut -d= -f2-" || true)
  lf=${lf_url##*:}; lf=${lf%%/*}
  case "$lf" in ''|*[!0-9]*) lf=3100 ;; esac
  echo "$lf ${TEMPORAL_UI_PORT:-8243}"
}

remote_wait_health() {
  be="$1"
  log_info "waiting for remote backend health..."
  i=0
  while [ "$i" -lt 60 ]; do
    if ssh_run "curl -fsS -o /dev/null http://localhost:$be/api/health" >/dev/null 2>&1; then
      log_ok "remote backend healthy"
      return 0
    fi
    sleep 1; i=$((i+1))
  done
  log_warn "remote backend health check timed out — continuing anyway"
}

close_stale_tunnel() {
  if [ -f "$TUNNEL_PID_FILE" ]; then
    pid=$(cat "$TUNNEL_PID_FILE" 2>/dev/null)
    if [ -n "$pid" ] && pid_alive "$pid"; then
      log_info "closing previous tunnel (pid $pid)"
      kill -TERM "$pid" 2>/dev/null || true
      sleep 0.3
      kill -KILL "$pid" 2>/dev/null || true
    fi
    rm -f "$TUNNEL_PID_FILE"
  fi
}

open_tunnel() {
  fe="$1"; be="$2"; db="$3"; lf="$4"; tui="$5"

  close_stale_tunnel

  ports="$fe $be"
  [ "$FORWARD_DB" = "1" ] && ports="$ports $db"

  for p in $ports; do
    ensure_port_free "$p" "$FORCE" || { log_err "port $p is blocked locally — resolve or pass --force"; exit 1; }
  done

  # SRE UIs are optional: skip one (don't abort) if its port is busy here,
  # e.g. because this machine also runs its own local SRE infra.
  sre_lf=""; sre_tui=""
  if [ -z "$(port_listener "$lf")" ]; then sre_lf="$lf"; ports="$ports $lf"
  else log_warn "local port $lf is busy — not forwarding the remote Langfuse (make infra-down here frees it)"; fi
  if [ -z "$(port_listener "$tui")" ]; then sre_tui="$tui"; ports="$ports $tui"
  else log_warn "local port $tui is busy — not forwarding the remote Temporal UI"; fi

  fwd_args=()
  for p in $ports; do
    fwd_args+=(-L "$p:localhost:$p")
  done

  log_info "opening SSH tunnel to $REMOTE_HOST"
  ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
    "${fwd_args[@]}" "$REMOTE_HOST" &
  tpid=$!
  echo "$tpid" > "$TUNNEL_PID_FILE"

  sleep 1
  if ! pid_alive "$tpid"; then
    log_err "tunnel failed to establish — check for port conflicts or SSH forwarding errors"
    rm -f "$TUNNEL_PID_FILE"
    exit 1
  fi

  echo
  log_ok "tunnel open:"
  printf '  %-10s http://localhost:%s\n' "frontend" "$fe"
  printf '  %-10s http://localhost:%s\n' "backend"  "$be"
  [ "$FORWARD_DB" = "1" ] && printf '  %-10s localhost:%s\n' "db" "$db"
  [ -n "$sre_lf" ] && printf '  %-10s http://localhost:%s  (SRE agent LLM traces)\n' "langfuse" "$sre_lf"
  [ -n "$sre_tui" ] && printf '  %-10s http://localhost:%s  (SRE workflows)\n' "temporal" "$sre_tui"
  echo
  log_info "Ctrl+C closes the tunnel only — the remote stack keeps running."
  log_info "stop the remote stack with: scripts/dev-tunnel.sh down"

  tunnel_cleanup() {
    log_info "closing tunnel"
    kill -TERM "$tpid" 2>/dev/null || true
    rm -f "$TUNNEL_PID_FILE"
    log_info "remote stack is still running; stop it with: scripts/dev-tunnel.sh down"
  }
  trap tunnel_cleanup INT TERM
  wait "$tpid" 2>/dev/null || true
  rm -f "$TUNNEL_PID_FILE"
}

case "$CMD" in
  up)
    preflight
    remote_ensure_checkout
    remote_ensure_env
    remote_start_stack
    read -r fe be <<EOF
$(remote_ports)
EOF
    read -r lf tui <<EOF
$(remote_sre_ports)
EOF
    remote_wait_health "$be"
    open_tunnel "$fe" "$be" "$(db_port)" "$lf" "$tui"
    ;;
  down)
    preflight
    log_info "stopping remote stack"
    ssh_run "cd $REMOTE_PROJECT_DIR && make stop" || true
    log_ok "remote stack stopped (if it was running)"
    ;;
  kill)
    preflight
    log_info "hard-stopping remote stack"
    ssh_run "cd $REMOTE_PROJECT_DIR && make stop ALL=1" || true
    ssh_run "cd $REMOTE_PROJECT_DIR && docker compose kill 2>/dev/null; docker compose down --remove-orphans 2>/dev/null; true" || true
    log_ok "remote stack killed (if it was running)"
    ;;
  tunnel)
    preflight
    read -r fe be <<EOF
$(remote_ports)
EOF
    read -r lf tui <<EOF
$(remote_sre_ports)
EOF
    open_tunnel "$fe" "$be" "$(db_port)" "$lf" "$tui"
    ;;
  status)
    preflight
    ssh_run "cd $REMOTE_PROJECT_DIR && make status"
    ;;
  logs)
    preflight
    if [ -n "$SVC" ]; then
      ssh -t "$REMOTE_HOST" "cd $REMOTE_PROJECT_DIR && make logs SERVICE=$SVC"
    else
      ssh -t "$REMOTE_HOST" "cd $REMOTE_PROJECT_DIR && make logs"
    fi
    ;;
esac
