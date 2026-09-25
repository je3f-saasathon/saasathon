<!-- AGENTS.md is the source of truth; run `make sync-agents` after editing. -->
# Agent conventions

A full-stack starter app: a React/Vite frontend, a Django/django-ninja backend, and optional standalone services. Layout: `frontend/`, `backend/` (+ `backend/services/`), `scripts/`, `docs/`. Contracts (every endpoint and data shape) live in `docs/CONTRACTS.md` — treat it as the source of truth for the API.

## Running it

`make setup` · `make dev` · `make dev-docker` · `make tunnel` · `make stop` · `make restart` · `make status` · `make doctor` · `make test` · `make contracts` · `make dev-token`

See `docs/SCRIPTS.md` for what each one does.

## Env rules

Each root (`frontend/`, `backend/`, each `backend/services/*`) has its own `.env`, documented by a committed `.env.example`. Never commit a real `.env`. Add new variables to that root's `.env.example` when you add them to code.

## Rules

- Only edit files inside your own area. If you need something from another area, update `docs/CONTRACTS.md` first and note the need.
- After changing backend endpoints or schemas, run `make contracts` to regenerate `backend/openapi.json` and the frontend's generated types.
- Use `make stop` / `make restart` rather than killing dev processes by hand.
- Python: `uv` only, never `pip`/`poetry`/`requirements.txt`/a manual venv. JS: `pnpm` only, never `npm`/`yarn`.
- No linters or formatters in this repo — don't add any.
- Commit small and often.
- Write or update a test for anything you add to auth or the API.

## Auth

Three login methods (email+password, GitHub OAuth, Google OAuth) all issue the same bearer token. Use `make dev-token` to get a token for local testing. See `docs/AUTH.md` for OAuth app setup.

## Where to find things

- `docs/TASKS.md` — backlog
- `docs/ARCHITECTURE.md` — design and ports
- `docs/CONTRACTS.md` — API source of truth
- `docs/DEPLOYMENT.md` — deploy/rollback/logs
- `docs/SCRIPTS.md` — what each script does
- `backend/sre/CLAUDE.md` — SRE agent (branch `feat/sre-agent`): read first for context, current state and next steps
