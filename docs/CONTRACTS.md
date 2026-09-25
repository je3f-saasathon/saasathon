# API Contracts

Source of truth for every endpoint. Backend and frontend must both match this. After changing backend endpoints, run `make contracts` to regenerate `backend/openapi.json` and the frontend's generated types.

Base URL: `VITE_API_URL` (dev: `http://localhost:8000`). All endpoints below are under `/api`.

## Auth

All auth endpoints are public except `/auth/me` and `/auth/logout` (require `Authorization: Bearer <token>`).

| Method | Path                        | Body / Params                        | Returns                          |
|--------|------------------------------|---------------------------------------|-----------------------------------|
| POST   | `/auth/register`             | `{email, password, name?}`            | `{token, user}`                   |
| POST   | `/auth/login`                | `{email, password}`                   | `{token, user}` or 401            |
| GET    | `/auth/github/login`         | —                                      | 302 redirect to GitHub            |
| GET    | `/auth/github/callback`      | `?code=&state=`                       | 302 redirect to `{FRONTEND_URL}/auth/callback#token=...` |
| GET    | `/auth/google/login`         | —                                      | 302 redirect to Google            |
| GET    | `/auth/google/callback`      | `?code=&state=`                       | 302 redirect to `{FRONTEND_URL}/auth/callback#token=...` |
| GET    | `/auth/me`                   | Bearer token                          | `{user}` or 401                   |
| POST   | `/auth/logout`               | Bearer token                          | `{ok: true}`, revokes token       |
| GET    | `/auth/providers`            | —                                      | `{password: true, github: bool, google: bool}` |

`user` shape: `{id, email, name, avatar_url, role, created_at}`

## Health

| Method | Path           | Returns                    |
|--------|-----------------|------------------------------|
| GET    | `/health`       | `{status: "ok", version}`   |

## Core entities (generic placeholder set — replace when IDEA is defined)

### Item

| Method | Path            | Body / Params        | Returns          |
|--------|------------------|------------------------|--------------------|
| GET    | `/items`         | `?page=&page_size=`   | `{items: [Item], total}` |
| POST   | `/items`         | `{title, description?}` | `Item`           |
| GET    | `/items/{id}`     | —                      | `Item` or 404      |
| PATCH  | `/items/{id}`     | `{title?, description?}` | `Item`          |
| DELETE | `/items/{id}`     | —                      | 204                |

`Item` shape: `{id, owner_id, title, description, created_at, updated_at}`. Ownership: users can only see/modify their own items (queryset filtered by `request.user`).

## Jev (Cloudflare Workers AI, experimental)

Requires `Authorization: Bearer <token>`. Requires `CLOUDFLARE_ACCOUNT_ID` / `CLOUDFLARE_API_TOKEN` to be set on the backend, and gateway balance (or BYOK) on the Cloudflare account — otherwise upstream calls fail and this returns 502.

| Method | Path        | Body / Params                                              | Returns                              |
|--------|-------------|--------------------------------------------------------------|----------------------------------------|
| POST   | `/jev/run`  | `{state: string, questions: {[name]: {type: "noul"\|"choice"\|"score", instructions, criteria}}}` | `{model, answers, usage}` or `502 {detail}` |

## Errors

All error responses: `{detail: string}` with the appropriate HTTP status. 401 for missing/invalid/expired token, 403 for wrong owner, 404 for missing resource, 422 for validation errors (django-ninja default).
