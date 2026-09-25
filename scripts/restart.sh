#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/lib/common.sh"

usage() {
  cat <<'EOF'
Usage: scripts/restart.sh [--docker] [--detach]

Stops whatever is running, then starts fresh. Default is native mode
(scripts/dev.sh); --docker uses scripts/dev-docker.sh.

  --docker   restart in docker mode
  --detach   pass through to dev-docker.sh (only meaningful with --docker)
  -h, --help
EOF
}

DOCKER=0
DETACH=0
for arg in "$@"; do
  case "$arg" in
    --docker) DOCKER=1 ;;
    --detach) DETACH=1 ;;
    -h|--help) usage; exit 0 ;;
    *) log_err "unknown arg: $arg"; usage; exit 1 ;;
  esac
done

"$(dirname "$0")/stop.sh"

if [ "$DOCKER" = "1" ]; then
  if [ "$DETACH" = "1" ]; then
    exec "$(dirname "$0")/dev-docker.sh" --detach
  else
    exec "$(dirname "$0")/dev-docker.sh"
  fi
else
  exec "$(dirname "$0")/dev.sh"
fi
