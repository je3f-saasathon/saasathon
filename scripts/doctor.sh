#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/lib/common.sh"

usage() {
  cat <<'EOF'
Usage: scripts/doctor.sh

Prints a green/red checklist of this project's dev environment: OS/bash
version, tool versions vs pinned versions, docker status, port usage,
.env presence, OAuth provider config, DB reachability, and stale .run/
pid files. Each red item includes its fix.
EOF
}
[ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ] && { usage; exit 0; }

RED=0
check() { # check <label> <ok:0|1> <fix-message>
  if [ "$2" = "0" ]; then log_ok "$1"; else log_err "$1"; [ -n "${3:-}" ] && log_err "    fix: $3"; RED=$((RED+1)); fi
}

log_info "OS: $(uname -s) $(uname -r), bash $BASH_VERSION"

# ---- tool versions -------------------------------------------------------
for tool in uv node pnpm docker; do
  if command -v "$tool" >/dev/null 2>&1; then
    check "$tool installed ($("$tool" --version 2>&1 | head -n1))" 0
  else
    check "$tool installed" 1 "run scripts/setup.sh"
  fi
done

if [ -f "$REPO_ROOT/frontend/.nvmrc" ] && command -v node >/dev/null 2>&1; then
  pinned=$(cat "$REPO_ROOT/frontend/.nvmrc")
  have=$(node --version | sed 's/^v//')
  case "$have" in
    "$pinned"*) check "node version matches .nvmrc ($pinned)" 0 ;;
    *) check "node version matches .nvmrc (pinned $pinned, have $have)" 1 "use nvm/fnm to install $pinned" ;;
  esac
fi

if [ -f "$REPO_ROOT/backend/.python-version" ] && command -v uv >/dev/null 2>&1; then
  pinned=$(cat "$REPO_ROOT/backend/.python-version")
  check "backend/.python-version present (pinned $pinned)" 0
fi

# ---- docker daemon --------------------------------------------------------
if command -v docker >/dev/null 2>&1; then
  if docker info >/dev/null 2>&1; then
    check "docker daemon running" 0
  else
    if is_mac; then fix="open -a Docker"; else fix="sudo systemctl start docker"; fi
    check "docker daemon running" 1 "$fix"
  fi
fi

# ---- ports -----------------------------------------------------------------
for pair in "frontend:$(frontend_port)" "backend:$(backend_port)" "db:$(db_port)"; do
  name=${pair%%:*}; port=${pair##*:}
  info=$(port_listener "$port")
  if [ -z "$info" ]; then
    log_ok "port $port ($name) free"
  else
    pid=${info%%|*}; cmd=${info##*|}
    log_warn "port $port ($name) in use by pid $pid ($cmd)"
  fi
done

# ---- env files ---------------------------------------------------------
if [ -f "$REPO_ROOT/frontend/.env" ]; then check "frontend/.env present" 0; else check "frontend/.env present" 1 "cp frontend/.env.example frontend/.env"; fi
if [ -f "$REPO_ROOT/backend/.env" ]; then check "backend/.env present" 0; else check "backend/.env present" 1 "cp backend/.env.example backend/.env"; fi

# ---- OAuth providers -----------------------------------------------------
if [ -f "$REPO_ROOT/backend/.env" ]; then
  gh_id=$(env_get "$REPO_ROOT/backend/.env" GITHUB_CLIENT_ID "")
  gh_secret=$(env_get "$REPO_ROOT/backend/.env" GITHUB_CLIENT_SECRET "")
  gg_id=$(env_get "$REPO_ROOT/backend/.env" GOOGLE_CLIENT_ID "")
  gg_secret=$(env_get "$REPO_ROOT/backend/.env" GOOGLE_CLIENT_SECRET "")
  if [ -n "$gh_id" ] && [ -n "$gh_secret" ]; then log_ok "GitHub OAuth configured"; else log_warn "GitHub OAuth not configured (see docs/AUTH.md)"; fi
  if [ -n "$gg_id" ] && [ -n "$gg_secret" ]; then log_ok "Google OAuth configured"; else log_warn "Google OAuth not configured (see docs/AUTH.md)"; fi
fi

# ---- DB reachability -----------------------------------------------------
if [ -f "$REPO_ROOT/backend/.env" ]; then
  db_url=$(env_get "$REPO_ROOT/backend/.env" DATABASE_URL "")
  case "$db_url" in
    postgres*)
      if port_listener "$(db_port)" >/dev/null; then check "postgres reachable on port $(db_port)" 0; else check "postgres reachable on port $(db_port)" 1 "scripts/dev-docker.sh --detach (or start postgres natively)"; fi
      ;;
    sqlite*|"") log_ok "using sqlite (no separate DB process needed)" ;;
  esac
fi

# ---- SRE agent -----------------------------------------------------------
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  sandbox_image=$(env_get "$REPO_ROOT/backend/.env" SRE_SANDBOX_IMAGE "saasathon-sre-sandbox:latest")
  if docker image inspect "$sandbox_image" >/dev/null 2>&1; then
    check "SRE sandbox image present ($sandbox_image)" 0
  else
    check "SRE sandbox image present ($sandbox_image)" 1 "make sandbox-image"
  fi
  health=$(docker compose -p saasathon-infra -f "$REPO_ROOT/docker-compose.infra.yml" ps --format '{{.Service}}={{.Health}}' 2>/dev/null || true)
  for svc in temporal langfuse-web; do
    case "$health" in
      *"$svc=healthy"*) log_ok "SRE infra: $svc healthy" ;;
      *) log_warn "SRE infra: $svc not running/healthy (make infra-up)" ;;
    esac
  done
  if [ -f "$REPO_ROOT/backend/.env" ] && [ -z "$(env_get "$REPO_ROOT/backend/.env" SRE_FIELD_ENCRYPTION_KEY "")" ]; then
    check "SRE_FIELD_ENCRYPTION_KEY set in backend/.env" 1 "uv run python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"  (run in backend/, paste into backend/.env)"
  fi
else
  log_warn "docker not available — SRE agent (Temporal, Langfuse, sandbox) can't run"
fi

# ---- stale pid files -------------------------------------------------------
if [ -d "$RUN_DIR" ]; then
  for pidfile in "$RUN_DIR"/*.pid; do
    [ -f "$pidfile" ] || continue
    pid=$(cat "$pidfile" 2>/dev/null)
    name=$(basename "$pidfile" .pid)
    if [ -n "$pid" ] && pid_alive "$pid"; then
      log_ok "$name running (pid $pid)"
    else
      check "$name pid file stale" 1 "rm $pidfile  (or run scripts/stop.sh)"
    fi
  done
fi

echo
if [ "$RED" -eq 0 ]; then
  log_ok "all checks passed"
else
  log_err "$RED check(s) need attention"
  exit 1
fi
