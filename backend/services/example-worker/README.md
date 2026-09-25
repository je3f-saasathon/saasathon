# example-worker

A minimal standalone HTTP service, meant as a template for adding a real
worker/model service to this monorepo. It isn't a feature on its own — copy
this folder to start a new one.

## What it is

- FastAPI app with two routes:
  - `GET /health` — no auth, returns `{status: "ok", version}`.
  - `POST /v1/example` — behind a shared-secret check, takes `{"text": "..."}`
    and returns a trivial transform (`{"result": "...", "length": N}`).
- Auth: shared secret via the `X-Service-Secret` request header, checked
  against the `SERVICE_SECRET` env var. The backend (or any caller) must send
  this header on every route except `/health`. There is no user-level auth
  here — this is a service-to-service check only.
- Port: `8100` by default (see `.env.example`), per the `8100+` scheme for
  `backend/services/*` in `docs/ARCHITECTURE.md`.

## Run standalone

```bash
cd backend/services/example-worker
cp .env.example .env
uv run --env-file .env uvicorn app.main:app --reload --port 8100
```

Then:

```bash
curl http://localhost:8100/health

curl -X POST http://localhost:8100/v1/example \
  -H "Content-Type: application/json" \
  -H "X-Service-Secret: change-me" \
  -d '{"text": "hello"}'
```

## Adding a real service

1. Copy this folder to `backend/services/<your-service-name>/`.
2. Update `pyproject.toml`'s `name`, add whatever dependencies you need.
3. Replace the body of `POST /v1/example` (and rename the route/models) with
   real logic; keep the `require_service_secret` dependency on any
   non-health route.
4. Pick the next free port in the `8100+` range and set it in your new
   `.env.example`.
5. Ask whoever owns `backend/pyproject.toml` / the compose files to wire the
   new service into the uv workspace and `docker-compose.yml`.
