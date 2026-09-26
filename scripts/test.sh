#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/lib/common.sh"

usage() {
  cat <<'EOF'
Usage: scripts/test.sh

Runs `uv run pytest` in backend/ and cli/, and `pnpm test` in frontend/.
Skips gracefully (with a warning) if a root isn't ready yet.
EOF
}
[ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ] && { usage; exit 0; }

FAIL=0

if [ -f "$REPO_ROOT/backend/pyproject.toml" ]; then
  log_info "running backend tests"
  if (cd "$REPO_ROOT/backend" && uv run pytest); then
    log_ok "backend tests passed"
  else
    log_err "backend tests failed"
    FAIL=1
  fi
else
  log_warn "backend/pyproject.toml not found yet — skipping backend tests"
fi

if [ -f "$REPO_ROOT/cli/pyproject.toml" ]; then
  log_info "running CLI tests"
  if (cd "$REPO_ROOT/cli" && uv run pytest); then
    log_ok "CLI tests passed"
  else
    log_err "CLI tests failed"
    FAIL=1
  fi
fi

if [ -f "$REPO_ROOT/frontend/package.json" ]; then
  log_info "running frontend tests"
  if (cd "$REPO_ROOT/frontend" && pnpm test); then
    log_ok "frontend tests passed"
  else
    log_err "frontend tests failed"
    FAIL=1
  fi
else
  log_warn "frontend/package.json not found yet — skipping frontend tests"
fi

exit $FAIL
