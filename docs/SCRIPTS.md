# Scripts

All commands below are `make` targets (thin wrappers around `scripts/*.sh`). Run them from the repo root.

## Everyday use

| Command | What it does |
|---|---|
| `make setup` | One-time bootstrap: installs deps, creates `.env` files, migrates DB, seeds demo user. Safe to re-run. |
| `make dev` | Runs frontend + backend natively (no Docker), SQLite. Ctrl+C stops everything. |
| `make dev-docker` | Runs the full stack in Docker (Postgres included). Add `DETACH=1` to run in the background. |
| `make tunnel` | Runs the stack on the remote host and tunnels it to `localhost` on your machine. |
| `make stop` | Stops whatever this repo started. Add `ALL=1` to also sweep stray processes/containers. |
| `make restart` | `stop` then `dev` (add `DETACH=1` for `dev-docker`, e.g. `make restart-docker DETACH=1`). |
| `make status` | Shows what's running, on which ports, and whether health checks pass. |
| `make doctor` | Full environment checklist (tool versions, ports, `.env` files, OAuth config, DB) with fixes for anything red. |

## Working with the API

| Command | What it does |
|---|---|
| `make test` | Runs backend (`pytest`) and frontend (`vitest`) test suites. |
| `make contracts` | Regenerates `backend/openapi.json` and the frontend's TypeScript API types. Run this after changing any backend endpoint. |
| `make dev-token` | Prints a bearer token for the demo user, for `curl`/Postman testing. Only works when `DEBUG=true`. |
| `make reset-db` | Wipes and recreates the dev database, then reseeds the demo user. |
| `make logs SERVICE=backend` | Tails logs for one service (`backend`, `frontend`, or a service name). Omit `SERVICE` to see all. |

## Deploys

| Command | What it does |
|---|---|
| `make deploy DRY_RUN=1` | Local rehearsal only — builds prod images and runs the prod health check on your machine. Real deploys happen automatically in CI on push to `main`. |
| `make prod-local` | Runs the production Docker Compose stack locally, so you can poke at it before it ships. |

## Remote tunnel

| Command | What it does |
|---|---|
| `make tunnel` | Starts the stack on the remote host (if not already running) and opens the tunnel. Ctrl+C closes the tunnel only — the remote stack keeps running. |
| `make tunnel CMD=down` | Stops the remote stack, no tunnel. |
| `make tunnel CMD=kill` | Force-stops the remote stack (use if `down` doesn't fully clean up). |
| `make tunnel CMD=status` | Shows what's running on the remote host. |
| `make tunnel CMD=logs SERVICE=backend` | Streams remote logs. |

First run copies `scripts/tunnel.env.example` to `scripts/tunnel.env` — edit that file with your remote host before using `make tunnel`.

## Notes

- `make setup YES=1` skips confirmation prompts (e.g. for CI or a fresh box).
- Nothing here kills processes it doesn't own without asking — if a port is blocked by something unrelated, the script tells you what it is and how to free it (or pass `FORCE=1`).
- There is no lint/format target on purpose — this repo doesn't use one.
