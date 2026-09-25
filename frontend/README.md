# frontend

React + TypeScript SPA built with Vite. Talks to the backend only through
`VITE_API_URL` and the typed API client in `src/api/`.

## Running

```sh
pnpm install
pnpm dev      # starts the Vite dev server (default port 5173)
pnpm test     # runs the Vitest suite
pnpm build    # type-checks and produces a production build in dist/
```

Node version is pinned in `.nvmrc`. Use pnpm (no npm/yarn) — `corepack enable`
if `pnpm` isn't on your PATH yet.

Copy `.env.example` to `.env` (or `.env.production.example` to `.env.production`
for a prod build) and adjust as needed.

## Environment variables

| Variable         | Description                                  |
|-------------------|-----------------------------------------------|
| `VITE_API_URL`    | Base URL of the backend API (baked in at build time) |
| `FRONTEND_PORT`   | Port the Vite dev server listens on          |

## Generating API types

`src/api/types.ts` is currently hand-written to match `docs/CONTRACTS.md`.
Once `backend/openapi.json` exists, regenerate it with:

```sh
pnpm gen:api
```
