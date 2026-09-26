#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/lib/common.sh"

usage() {
  cat <<'EOF2'
Usage: scripts/langfuse.sh up|down|status|logs|backend-env

Manages the production Langfuse (infra/langfuse/docker-compose.yml, compose project
"saasathon-langfuse"): the SRE worker's LLM traces. Served publicly as
https://langfuse.buggly.dev through the Cloudflare tunnel. (Local dev uses the Langfuse
in docker-compose.infra.yml instead.)

  up           create infra/langfuse/.env with random secrets if missing, start, wait for health
  down         stop the containers (data volumes are kept)
  status       show the containers
  logs         follow the web container's logs
  backend-env  print the LANGFUSE_* lines for the prod backend's .env.prod (contains the secret key)
EOF2
}

DIR="$REPO_ROOT/infra/langfuse"
ENV_FILE="$DIR/.env"
LANGFUSE=(docker compose -p saasathon-langfuse -f "$DIR/docker-compose.yml")

require_docker() {
  if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
    log_err "docker is not available — Langfuse needs it"
    exit 1
  fi
}

set_if_blank() {  # key value
  if grep -qE "^$1=$" "$ENV_FILE"; then
    sed -i.bak "s|^$1=$|$1=$2|" "$ENV_FILE" && rm -f "$ENV_FILE.bak"
    log_info "generated $1"
  fi
}

# Copies .env.example and fills each blank secret with a random value. Existing values
# are never changed, so rerunning is safe (Langfuse only reads the INIT_* ones on first start).
ensure_langfuse_env() {
  if [ ! -f "$ENV_FILE" ]; then
    cp "$DIR/.env.example" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    log_info "created infra/langfuse/.env"
  fi
  for key in LANGFUSE_ADMIN_PASSWORD LANGFUSE_NEXTAUTH_SECRET LANGFUSE_SALT LANGFUSE_DB_PASSWORD \
             LANGFUSE_CLICKHOUSE_PASSWORD LANGFUSE_MINIO_PASSWORD LANGFUSE_REDIS_PASSWORD; do
    set_if_blank "$key" "$(openssl rand -hex 24)"
  done
  set_if_blank LANGFUSE_ENCRYPTION_KEY "$(openssl rand -hex 32)"
  set_if_blank LANGFUSE_PUBLIC_KEY "pk-lf-$(openssl rand -hex 16)"
  set_if_blank LANGFUSE_SECRET_KEY "sk-lf-$(openssl rand -hex 24)"
}

case "${1:-}" in
  up)
    require_docker
    require_cmd openssl
    ensure_langfuse_env
    port=$(env_get "$ENV_FILE" LANGFUSE_PORT 14420)
    log_info "starting Langfuse (first start runs migrations and takes a few minutes)"
    "${LANGFUSE[@]}" up -d
    if wait_for_http "http://127.0.0.1:$port/api/public/health" 300; then
      log_ok "langfuse up"
    else
      log_warn "langfuse not healthy yet (scripts/langfuse.sh logs)"
    fi
    printf '  %-10s %s\n' "local" "http://127.0.0.1:$port"
    printf '  %-10s %s\n' "public" "$(env_get "$ENV_FILE" LANGFUSE_URL "")"
    printf '  %-10s %s  (password in infra/langfuse/.env)\n' "login" "$(env_get "$ENV_FILE" LANGFUSE_ADMIN_EMAIL "")"
    ;;
  down)
    require_docker
    "${LANGFUSE[@]}" down
    log_ok "Langfuse stopped (volumes kept)"
    ;;
  status)
    require_docker
    "${LANGFUSE[@]}" ps
    ;;
  logs)
    require_docker
    "${LANGFUSE[@]}" logs -f --tail 100 web
    ;;
  backend-env)
    [ -f "$ENV_FILE" ] || { log_err "no infra/langfuse/.env yet: run scripts/langfuse.sh up"; exit 1; }
    printf 'LANGFUSE_HOST=%s\n' "$(env_get "$ENV_FILE" LANGFUSE_URL "")"
    printf 'LANGFUSE_PUBLIC_KEY=%s\n' "$(env_get "$ENV_FILE" LANGFUSE_PUBLIC_KEY "")"
    printf 'LANGFUSE_SECRET_KEY=%s\n' "$(env_get "$ENV_FILE" LANGFUSE_SECRET_KEY "")"
    ;;
  -h|--help) usage ;;
  *) usage; exit 1 ;;
esac
