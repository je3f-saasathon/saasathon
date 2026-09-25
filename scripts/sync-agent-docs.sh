#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/lib/common.sh"

usage() {
  cat <<'EOF'
Usage: scripts/sync-agent-docs.sh

Copies the root AGENTS.md over CLAUDE.md, byte-for-byte, so both AI
coding assistants read the same project instructions.
EOF
}
[ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ] && { usage; exit 0; }

src="$REPO_ROOT/AGENTS.md"
dst="$REPO_ROOT/CLAUDE.md"

if [ ! -f "$src" ]; then
  log_err "$src not found"
  exit 1
fi

cp "$src" "$dst"
log_ok "copied AGENTS.md -> CLAUDE.md"
