# Architecture

## Components

- **frontend/** — React + TypeScript + Vite SPA. Talks to the backend only via `VITE_API_URL` and generated OpenAPI types. No server-side rendering.
- **backend/** — Django 5 + django-ninja API. Owns all persistence, auth, and business logic. Exposes a typed OpenAPI schema at `/api/openapi.json`.
- **cli/** — the `buggly` command-line tool (`buggly login`, `buggly run python app.py`): runs a user's app with OpenTelemetry auto-instrumentation and a crash hook, sending its errors to its project's managed Uptrace. Its own `uv` project; see `cli/README.md`.
- **backend/services/** — optional standalone worker/model services, one folder per service, each a narrow HTTP interface behind a shared-secret or bearer check. Not used unless a feature needs isolated compute.
- **backend/sre/** — the LLM SRE agent: a Django app (projects, members, LLM configs, playbooks, incident runs) plus a Temporal worker process (`python -m sre.worker`) that runs `IncidentDiagnosisWorkflow`. Unlike `backend/services/*` it is **not** an isolated HTTP service: the worker imports the Django ORM directly, because its data is first-party tenant data and an HTTP hop back to our own API would add plumbing without isolation. The isolation that matters — untrusted LLM-driven code execution — happens in a separate sandbox container per attempt. Why these calls were made: `docs/tradeoffs.md`.
- **docker-compose.infra.yml** — local SRE infra, its own compose project (`saasathon-infra`): Temporal (dev server) and self-hosted Langfuse (web, worker, Postgres, ClickHouse, Redis, MinIO). `make dev` starts it and leaves it running; `make infra-down` stops it.
- **infra/langfuse/** — the production Langfuse (the SRE worker's LLM traces), its own compose project (`saasathon-langfuse`) with Postgres, ClickHouse, Redis and MinIO. Bound to `127.0.0.1:14420`; the Cloudflare tunnel serves it as `https://langfuse.buggly.dev`. Sign-up is off. `make langfuse-up` / `make langfuse-down`. Local dev keeps using the Langfuse in `docker-compose.infra.yml`.
- **infra/uptrace/** — self-hosted Uptrace (traces, logs, metrics and the alerts that start the SRE agent), its own compose project (`saasathon-uptrace`) with ClickHouse, Postgres and Redis. Bound to `127.0.0.1:14418` (UI, API, OTLP/HTTP) and `:14417` (OTLP/gRPC); the Cloudflare tunnel serves it as `https://uptrace.buggly.dev`. `make uptrace-up` / `make uptrace-down`.
- **scripts/** — dev/ops tooling shared across roots (start/stop/status/tunnel/deploy).

## Data flow

```
Browser --HTTPS--> frontend (static build / Vite dev server)
Browser --HTTPS Bearer token--> backend (/api/*)
backend --SQL--> database (SQLite dev / Postgres docker+prod)
backend --HTTP + shared secret--> services/* (optional)

Uptrace --HTTPS webhook (HMAC or shared secret)--> backend (/api/sre/webhooks/uptrace/{project})
backend --gRPC start workflow / approval signal--> Temporal
sre worker --gRPC poll--> Temporal
sre worker --SQL (Django ORM)--> database
sre worker --HTTPS--> LLM providers (Anthropic / OpenAI / self-hosted / Cloudflare Jev)
sre worker --OTel via Langfuse SDK--> Langfuse (one trace per LLM step, session per incident)
sre worker --Docker API--> sandbox container (repo work tree only: no secrets, no network)
sre worker --HTTPS (GitHub App installation token)--> GitHub (clone, push branch, open PR)
```

With managed Uptrace (`UPTRACE_MANAGED_TOKEN` set), the platform creates that webhook itself: each project gets an Uptrace project (or shares one, split by service name), an error monitor on exception events and a webhook channel, set up by `UptraceSyncWorkflow` through Uptrace's internal API (`backend/sre/services/uptrace_admin.py`). Users only get a DSN to export telemetry to, over OTLP/HTTP.

The sandbox is where LLM-chosen commands run. The worker holds every secret (DB, GitHub App key, users' LLM keys), so it never runs agent tool calls itself, keeps git metadata outside the sandbox's mount, and runs git with hooks/fsmonitor disabled.

Auth tokens are opaque bearer tokens issued by the backend regardless of login method (email/password, GitHub OAuth, Google OAuth). No cookies, no CSRF handling needed on the API.

## Ports (defaults, overridable via each root's `.env`)

| Component        | Port | Source                          |
|-------------------|------|----------------------------------|
| frontend (Vite)   | 5173 | `frontend/.env` `FRONTEND_PORT`  |
| backend (Django)  | 8000 | `backend/.env` `BACKEND_PORT`    |
| Postgres (docker) | 5432 | `backend/.env` `DATABASE_URL`    |
| services/*        | 8100+, one per service, incrementing | each service's `.env` |
| Temporal gRPC     | 7243 (not 7233, to coexist with other projects) | `TEMPORAL_PORT` / `backend/.env` `TEMPORAL_ADDRESS` |
| Temporal UI       | 8243 | `TEMPORAL_UI_PORT` |
| Langfuse          | 3100 | `LANGFUSE_PORT` / `backend/.env` `LANGFUSE_HOST` |

## Environments

- **native**: pnpm + uv directly on the host, SQLite. The SRE worker runs natively too and starts sandboxes on the host's Docker; Temporal/Langfuse run in `docker-compose.infra.yml`.
- **docker**: full stack via `docker-compose.yml`, Postgres included, bind-mounted for hot reload. Adds an `sre-worker` container that reaches the infra via `host.docker.internal` and starts sandboxes through the mounted host Docker socket (local only).
- **remote tunnel**: stack runs on the shared dev/prod box, ports forwarded back to the developer's machine via SSH so all URLs stay `localhost`.

## Production topology

Production runs the same containers as `docker-compose.prod.yml` (including `sre-worker` and an `sre-docker` docker:dind sidecar that sandboxes run on, so a sandbox escape doesn't land on the host) (no bind mounts, process managers instead of dev servers) behind a tunnel that maps public hostnames to localhost ports. CI builds and tests on every push; on green, each root deploys independently.
