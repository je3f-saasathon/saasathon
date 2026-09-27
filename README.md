# saasathon

> **Archived.** This project is no longer maintained or hosted: buggly.dev and its API are offline, and the `buggly` CLI no longer works against it. The code is kept here for reference and can still be run locally with the steps below.

A full-stack starter: React/Vite frontend, Django/django-ninja backend, working auth (email+password, GitHub, Google), and dev tooling for native, Docker, and remote-tunnel workflows.

## Quickstart

```
git clone <this-repo>
cd saasathon
make setup
make dev
```

Frontend: http://localhost:5173 · Backend: http://localhost:8000 · API docs: http://localhost:8000/api/docs

## Run modes

- **Native** (no Docker): `make dev` — pnpm + uv directly on your machine, SQLite.
- **Docker**: `make dev-docker` — full stack via Compose, Postgres included, hot reload.
- **Remote tunnel**: `make tunnel` — stack runs on a shared remote box, forwarded back to `localhost`. Edit `scripts/tunnel.env` (copied from `scripts/tunnel.env.example` on first run) first.

All three expose the same `localhost` URLs and ports.

See `docs/SCRIPTS.md` for the full command reference.

## Structure

```
frontend/   React + TypeScript + Vite
backend/    Django + django-ninja API, plus optional backend/services/*
scripts/    dev/ops tooling (see docs/SCRIPTS.md)
docs/       architecture, API contracts, auth setup, deployment, backlog
```

## Auth

Email+password works out of the box. GitHub and Google logins need OAuth app credentials — see `docs/AUTH.md`. `make dev-token` mints a bearer token for the seeded demo user for local API testing.

## Troubleshooting

- `make doctor` — full environment checklist with fixes for anything red.
- Port already in use / stack won't start cleanly: `make stop ALL=1`, then retry.
- Backend/frontend types out of sync: `make contracts`.

## Docs

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- [docs/CONTRACTS.md](docs/CONTRACTS.md)
- [docs/AUTH.md](docs/AUTH.md)
- [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)
- [docs/SCRIPTS.md](docs/SCRIPTS.md)
- [docs/TASKS.md](docs/TASKS.md)
