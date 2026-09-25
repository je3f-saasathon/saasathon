#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/lib/common.sh"

usage() {
  cat <<'EOF'
Usage: scripts/deploy.sh [--dry-run]

LOCAL REHEARSAL ONLY. Real deploys are CI-driven (see
.github/workflows/deploy-backend.yml and deploy-frontend.yml), triggered
automatically after CI goes green on main. This script builds the prod
images locally and runs the same health checks against
docker-compose.prod.yml so you can catch build/runtime issues before
pushing.

  --dry-run   print the commands that would run, without executing them
  -h, --help
EOF
}

DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help) usage; exit 0 ;;
    *) log_err "unknown arg: $arg"; usage; exit 1 ;;
  esac
done

run() {
  if [ "$DRY_RUN" = "1" ]; then
    echo "+ $*"
  else
    "$@"
  fi
}

if ! command -v docker >/dev/null 2>&1; then
  log_err "docker is required for a local deploy rehearsal"
  exit 1
fi

ENV_FILE_DIR="${ENV_FILE_DIR:-$HOME/prod-saasathon}"
BE_ENV="$ENV_FILE_DIR/backend/.env.prod"
FE_ENV="$ENV_FILE_DIR/frontend/.env.prod"

if [ "$DRY_RUN" = "0" ] && { [ ! -f "$BE_ENV" ] || [ ! -f "$FE_ENV" ]; }; then
  log_err "missing $BE_ENV or $FE_ENV"
  log_err "  create them from backend/.env.production.example and frontend/.env.production.example"
  log_err "  (or set ENV_FILE_DIR to point somewhere else)"
  exit 1
fi

# Compose only reads ${VAR} interpolation (ports:, build.args:) from the
# shell environment or --env-file, never from a service's env_file:. Export
# both roles' vars into this shell so ports/build-args resolve consistently
# no matter which services a given `up`/`build` call touches (compose
# recreates dependencies too, e.g. `up frontend` also touches `backend` via
# depends_on, using whatever interpolation context is active at the time).
export ENV_FILE_DIR
export BACKEND_PORT=$(env_get "$BE_ENV" BACKEND_PORT 8000)
export FRONTEND_PORT=$(env_get "$FE_ENV" FRONTEND_PORT 5173)
export VITE_API_URL=$(env_get "$FE_ENV" VITE_API_URL http://localhost:8000)

COMPOSE=(docker compose -p saasathon-prod-local -f "$REPO_ROOT/docker-compose.prod.yml")

log_info "building prod images"
run "${COMPOSE[@]}" build

log_info "starting prod stack locally"
run "${COMPOSE[@]}" up -d

BE_PORT="$BACKEND_PORT"
FE_PORT="$FRONTEND_PORT"

# Prod ALLOWED_HOSTS only permits the real hostnames, so health checks (like
# the real CI deploy workflows) must send the matching Host header — plain
# `curl localhost:$PORT` gets a 400 from Django otherwise.
BE_HOST=$(env_get "$BE_ENV" ALLOWED_HOSTS "localhost")
BE_HOST=${BE_HOST%%,*}
FE_HOST=$(python3 -c "from urllib.parse import urlparse; print(urlparse('$(env_get "$BE_ENV" FRONTEND_URL "http://localhost")').hostname or 'localhost')" 2>/dev/null || echo localhost)

wait_for_http_host() {
  url="$1"; host="$2"; timeout="${3:-60}"
  i=0
  while [ "$i" -lt "$timeout" ]; do
    curl -fsS -H "Host: $host" -o /dev/null "$url" 2>/dev/null && return 0
    sleep 1
    i=$((i+1))
  done
  return 1
}

if [ "$DRY_RUN" = "0" ]; then
  log_info "waiting for backend health"
  if wait_for_http_host "http://localhost:$BE_PORT/api/health" "$BE_HOST" 60; then
    log_ok "backend healthy"
  else
    log_err "backend failed local prod health check"
    "${COMPOSE[@]}" logs --tail 100
    "${COMPOSE[@]}" down
    exit 1
  fi
  log_info "waiting for frontend"
  if wait_for_http_host "http://localhost:$FE_PORT" "$FE_HOST" 60; then
    log_ok "frontend healthy"
  else
    log_err "frontend failed local prod health check"
    "${COMPOSE[@]}" logs --tail 100
    "${COMPOSE[@]}" down
    exit 1
  fi
  log_ok "local prod rehearsal passed"
  log_info "tear down with: docker compose -p saasathon-prod-local -f docker-compose.prod.yml down"
else
  log_info "(dry run — nothing was started, no health checks were run)"
fi
