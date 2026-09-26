#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/lib/common.sh"

usage() {
  cat <<'EOF'
Usage: scripts/uptrace.sh up|down|status|logs

Manages saasathon's self-hosted Uptrace (infra/uptrace/docker-compose.yml, compose
project "saasathon-uptrace"): traces, logs and metrics, and the alerts that start the
SRE agent. Served publicly as https://uptrace.buggly.dev through the Cloudflare tunnel.

  up      create infra/uptrace/.env with random secrets if missing, start, wait for the UI
  down    stop the containers (data volumes are kept)
  status  show the containers
  logs    follow Uptrace's logs
EOF
}

DIR="$REPO_ROOT/infra/uptrace"
ENV_FILE="$DIR/.env"
UPTRACE=(docker compose -p saasathon-uptrace -f "$DIR/docker-compose.yml")

require_docker() {
  if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
    log_err "docker is not available — Uptrace needs it"
    exit 1
  fi
}

# Copies .env.example and fills each blank secret with a random value. Existing values
# are never changed, so rerunning is safe.
ensure_uptrace_env() {
  if [ ! -f "$ENV_FILE" ]; then
    cp "$DIR/.env.example" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    log_info "created infra/uptrace/.env"
  fi
  for key in UPTRACE_SECRET UPTRACE_ADMIN_PASSWORD UPTRACE_API_TOKEN UPTRACE_PROJECT_TOKEN \
             UPTRACE_CH_PASSWORD UPTRACE_PG_PASSWORD; do
    if grep -qE "^$key=$" "$ENV_FILE"; then
      value=$(openssl rand -hex 24)
      sed -i.bak "s|^$key=$|$key=$value|" "$ENV_FILE" && rm -f "$ENV_FILE.bak"
      log_info "generated $key"
    fi
  done
}

case "${1:-}" in
  up)
    require_docker
    require_cmd openssl
    ensure_uptrace_env
    port=$(env_get "$ENV_FILE" UPTRACE_HTTP_PORT 14418)
    log_info "starting Uptrace"
    "${UPTRACE[@]}" up -d
    if wait_for_http "http://127.0.0.1:$port" 120; then
      log_ok "uptrace up"
    else
      log_warn "uptrace not answering yet (scripts/uptrace.sh logs)"
    fi
    printf '  %-10s %s\n' "local" "http://127.0.0.1:$port"
    printf '  %-10s %s\n' "public" "$(env_get "$ENV_FILE" UPTRACE_SITE_URL "")"
    printf '  %-10s %s  (password in infra/uptrace/.env)\n' "login" "$(env_get "$ENV_FILE" UPTRACE_ADMIN_EMAIL "")"
    ;;
  down)
    require_docker
    "${UPTRACE[@]}" down
    log_ok "Uptrace stopped (volumes kept)"
    ;;
  status)
    require_docker
    "${UPTRACE[@]}" ps
    ;;
  logs)
    require_docker
    "${UPTRACE[@]}" logs -f --tail 100 uptrace
    ;;
  -h|--help) usage ;;
  *) usage; exit 1 ;;
esac
