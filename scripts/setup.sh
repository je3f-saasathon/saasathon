#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/lib/common.sh"

usage() {
  cat <<'EOF'
Usage: scripts/setup.sh [--docker-only|--native-only] [--yes]

Checks for (and installs, or prints the exact install command for) the
tools this project needs, installs dependencies in frontend/ and backend/
if present, creates .env files from .env.example, and runs migrations +
demo-user seeding if the backend is ready. Safe to re-run.

  --docker-only   only verify/print docker install guidance, skip native toolchain
  --native-only   only verify uv/node/pnpm, skip docker
  --yes           allow this script to actually run install commands (via sudo/brew)
                   instead of just printing them
  -h, --help      show this help
EOF
}

MODE="both"
AUTO_YES=0
for arg in "$@"; do
  case "$arg" in
    --docker-only) MODE="docker" ;;
    --native-only) MODE="native" ;;
    --yes) AUTO_YES=1 ;;
    -h|--help) usage; exit 0 ;;
    *) log_err "unknown arg: $arg"; usage; exit 1 ;;
  esac
done

install_hint() {
  tool="$1"
  if is_mac; then
    case "$tool" in
      uv)     echo "brew install uv" ;;
      node)   echo "brew install node@20" ;;
      pnpm)   echo "brew install pnpm" ;;
      docker) echo "brew install --cask docker" ;;
    esac
  else
    if command -v apt >/dev/null 2>&1; then
      case "$tool" in
        uv)     echo "curl -LsSf https://astral.sh/uv/install.sh | sh" ;;
        node)   echo "curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - && sudo apt install -y nodejs" ;;
        pnpm)   echo "sudo corepack enable && corepack prepare pnpm@latest --activate" ;;
        docker) echo "sudo apt install -y docker.io docker-compose-plugin" ;;
      esac
    elif command -v dnf >/dev/null 2>&1; then
      case "$tool" in
        uv)     echo "curl -LsSf https://astral.sh/uv/install.sh | sh" ;;
        node)   echo "sudo dnf install -y nodejs" ;;
        pnpm)   echo "sudo corepack enable && corepack prepare pnpm@latest --activate" ;;
        docker) echo "sudo dnf install -y docker docker-compose-plugin" ;;
      esac
    else
      case "$tool" in
        uv)     echo "curl -LsSf https://astral.sh/uv/install.sh | sh" ;;
        node)   echo "install Node 20 via your distro's package manager" ;;
        pnpm)   echo "corepack enable && corepack prepare pnpm@latest --activate" ;;
        docker) echo "install docker + the compose plugin via your distro's package manager" ;;
      esac
    fi
  fi
}

ensure_tool() {
  tool="$1"
  if command -v "$tool" >/dev/null 2>&1; then
    log_ok "$tool found: $("$tool" --version 2>&1 | head -n1)"
    return 0
  fi
  hint=$(install_hint "$tool")
  if [ "$AUTO_YES" = "1" ]; then
    log_info "installing $tool: $hint"
    eval "$hint"
  else
    log_warn "$tool not found. To install, run:"
    log_warn "  $hint"
    return 1
  fi
}

if [ "$MODE" = "both" ] || [ "$MODE" = "native" ]; then
  ensure_tool uv || true
  ensure_tool node || true
  ensure_tool pnpm || true
fi

if [ "$MODE" = "both" ] || [ "$MODE" = "docker" ]; then
  ensure_tool docker || true
fi

# ---- backend -----------------------------------------------------------
if [ -f "$REPO_ROOT/backend/pyproject.toml" ]; then
  ensure_env_file "$REPO_ROOT/backend"
  # The SRE agent can't store LLM keys without this; generate one if unset.
  if [ -z "$(env_get "$REPO_ROOT/backend/.env" SRE_FIELD_ENCRYPTION_KEY "")" ] && command -v python3 >/dev/null 2>&1; then
    sre_key=$(python3 -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())")
    printf '\nSRE_FIELD_ENCRYPTION_KEY=%s\n' "$sre_key" >> "$REPO_ROOT/backend/.env"
    log_ok "generated SRE_FIELD_ENCRYPTION_KEY in backend/.env (keep it: losing it makes stored LLM keys unreadable)"
  fi
  if command -v uv >/dev/null 2>&1; then
    log_info "uv sync in backend/"
    (cd "$REPO_ROOT/backend" && uv sync)
    log_ok "backend dependencies synced"
    if [ -f "$REPO_ROOT/backend/manage.py" ]; then
      log_info "running backend migrations"
      (cd "$REPO_ROOT/backend" && uv run python manage.py migrate) || log_warn "migrations failed (backend may still be under construction)"
      log_info "seeding demo user"
      (cd "$REPO_ROOT/backend" && uv run python manage.py seed_demo_user) || log_warn "seed_demo_user not available yet"
    fi
  else
    log_warn "uv not available, skipping backend dependency install"
  fi
else
  log_warn "backend/pyproject.toml not found yet — skipping backend setup"
fi

# ---- frontend ------------------------------------------------------------
if [ -f "$REPO_ROOT/frontend/package.json" ]; then
  ensure_env_file "$REPO_ROOT/frontend"
  if command -v pnpm >/dev/null 2>&1; then
    log_info "pnpm install in frontend/"
    (cd "$REPO_ROOT/frontend" && pnpm install)
    log_ok "frontend dependencies installed"
  else
    log_warn "pnpm not available, skipping frontend dependency install"
  fi
else
  log_warn "frontend/package.json not found yet — skipping frontend setup"
fi

log_ok "setup complete"
