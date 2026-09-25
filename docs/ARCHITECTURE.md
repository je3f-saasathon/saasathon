# Architecture

## Components

- **frontend/** — React + TypeScript + Vite SPA. Talks to the backend only via `VITE_API_URL` and generated OpenAPI types. No server-side rendering.
- **backend/** — Django 5 + django-ninja API. Owns all persistence, auth, and business logic. Exposes a typed OpenAPI schema at `/api/openapi.json`.
- **backend/services/** — optional standalone worker/model services, one folder per service, each a narrow HTTP interface behind a shared-secret or bearer check. Not used unless a feature needs isolated compute.
- **scripts/** — dev/ops tooling shared across roots (start/stop/status/tunnel/deploy).

## Data flow

```
Browser --HTTPS--> frontend (static build / Vite dev server)
Browser --HTTPS Bearer token--> backend (/api/*)
backend --SQL--> database (SQLite dev / Postgres docker+prod)
backend --HTTP + shared secret--> services/* (optional)
```

Auth tokens are opaque bearer tokens issued by the backend regardless of login method (email/password, GitHub OAuth, Google OAuth). No cookies, no CSRF handling needed on the API.

## Ports (defaults, overridable via each root's `.env`)

| Component        | Port | Source                          |
|-------------------|------|----------------------------------|
| frontend (Vite)   | 5173 | `frontend/.env` `FRONTEND_PORT`  |
| backend (Django)  | 8000 | `backend/.env` `BACKEND_PORT`    |
| Postgres (docker) | 5432 | `backend/.env` `DATABASE_URL`    |
| services/*        | 8100+, one per service, incrementing | each service's `.env` |

## Environments

- **native**: pnpm + uv directly on the host, SQLite.
- **docker**: full stack via `docker-compose.yml`, Postgres included, bind-mounted for hot reload.
- **remote tunnel**: stack runs on the shared dev/prod box, ports forwarded back to the developer's machine via SSH so all URLs stay `localhost`.

## Production topology

Production runs the same containers as `docker-compose.prod.yml` (no bind mounts, process managers instead of dev servers) behind a tunnel that maps public hostnames to localhost ports. CI builds and tests on every push; on green, each root deploys independently.
