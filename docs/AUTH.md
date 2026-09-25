# Auth setup

## GitHub OAuth App

GitHub Settings > Developer settings > OAuth Apps > New OAuth App.

- Local dev callback: `http://localhost:8000/api/auth/github/callback`
- Production callback: `https://api.dev.andrewplescan.com/api/auth/github/callback`

Set `GITHUB_CLIENT_ID` and `GITHUB_CLIENT_SECRET` in `backend/.env` (or `.env.prod`).

## Google OAuth client

Google Cloud Console > APIs & Services > Credentials > Create Credentials >
OAuth client ID > Application type "Web application".

- Local dev callback: `http://localhost:8000/api/auth/google/callback`
- Production callback: `https://api.dev.andrewplescan.com/api/auth/google/callback`

Set `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` in `backend/.env` (or `.env.prod`).

Both callback URLs work unchanged through `scripts/dev-tunnel.sh`, since it forwards
the same ports back to `localhost`.

## Account linking

Accounts are linked by verified email: if a user signs in with GitHub or Google
using an email that already exists (password or another provider), the login is
attached to that existing user rather than creating a duplicate.

## Dev tokens

`make dev-token` mints a bearer token for the demo user via a Django management
command, for local API testing. It refuses to run unless backend `DEBUG=true` and
must never be exposed over HTTP.
