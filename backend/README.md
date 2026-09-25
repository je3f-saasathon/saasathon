# backend

Django 5 + django-ninja API. Owns persistence, auth, and business logic for
the project. Exposes a typed OpenAPI schema at `/api/openapi.json` (raw
schema also checked in at `backend/openapi.json`, regenerated via
`export_openapi`).

See `../docs/ARCHITECTURE.md` and `../docs/CONTRACTS.md` for the full
picture and the endpoint-by-endpoint contract.

## Requirements

- Python, pinned via `.python-version` (managed automatically by `uv`).
- [uv](https://docs.astral.sh/uv/) for all dependency management — no pip,
  poetry, or manual virtualenvs.

## Setup

```sh
cd backend
cp .env.example .env        # adjust values as needed
uv sync
uv run python manage.py migrate
uv run python manage.py createsuperuser   # optional, for /admin
uv run python manage.py seed_demo_user    # optional, only works when DEBUG=true
uv run python manage.py runserver
```

The API is served at `http://localhost:8000/api/` (see `BACKEND_PORT` in
`.env` to change the port).

## Running tests

```sh
uv run pytest
```

Tests use `pytest-django` with an in-memory SQLite database and mock all
outbound GitHub/Google HTTP calls with the `responses` library — no real
network access or OAuth app credentials required to run the suite.

## Regenerating the OpenAPI schema

Whenever an endpoint changes, regenerate the committed schema:

```sh
uv run python manage.py export_openapi
```

This writes `backend/openapi.json`, which the frontend's typegen consumes.

## Project layout

- `config/` — Django project: settings (`config/settings/base.py` +
  `dev.py` / `prod.py` / `test.py`), root URLconf, and the top-level
  django-ninja `NinjaAPI` instance (`config/api.py`).
- `accounts/` — custom `User` model, opaque bearer `AuthToken`, the
  `HttpBearer` auth class, and all `/api/auth/*` endpoints including
  GitHub/Google OAuth.
- `services/` — optional standalone worker/model services (owned
  separately; declared as a `uv` workspace so each service manages its
  own dependencies).

## Environment variables

See `.env.example` (dev) and `.env.production.example` (prod) for the full,
documented list. Highlights:

| Var | Purpose |
|---|---|
| `SECRET_KEY` | Django secret key |
| `DEBUG` | Enables debug mode + `seed_demo_user` |
| `DATABASE_URL` | SQLite by default, Postgres via URL override |
| `FRONTEND_URL` | Where OAuth redirects send the browser back to |
| `CORS_ALLOWED_ORIGINS` | Comma-separated allowed origins |
| `TOKEN_TTL_DAYS` | Bearer token lifetime |
| `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` / `GITHUB_REDIRECT_URI` | GitHub OAuth app; login disabled if unset |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` / `GOOGLE_REDIRECT_URI` | Google OAuth client; login disabled if unset |

## Auth model

- Email + password is always available (`/api/auth/register`,
  `/api/auth/login`), using Django's default password hashers/validators.
- GitHub and Google OAuth (Authorization Code flow) are available whenever
  their client id + secret are both set; `/api/auth/providers` reports
  which are enabled, and a disabled provider's login endpoint returns a
  `400` pointing at `docs/AUTH.md`.
- Every login method (password or OAuth) issues the same kind of opaque
  bearer token. Only a SHA-256 hash of the token is stored; the raw value
  is returned exactly once, at issuance.
- Account linking happens by verified email: a verified OAuth email that
  matches an existing user links that provider's id to the existing
  account instead of creating a duplicate. Unverified emails are never
  linked. OAuth-only users get an unusable password.

## Docker

```sh
docker build --target dev  -t backend:dev  backend/
docker build --target prod -t backend:prod backend/
```

The `dev` target runs `manage.py runserver` and is meant to be bind-mounted
for hot reload. The `prod` target runs `gunicorn` with `uvicorn` workers and
no bind mount.
