# Task Backlog

## Frontend
-

## Backend
-

## Services
-

## Tooling / Infra
-

## Known gaps from scaffolding
- Login-attempt throttling (per IP + email) is implemented via Django's LocMemCache (`accounts/api.py`, 5 attempts / 60s). Fine for a single dev/demo process; swap to a shared cache backend (Redis, etc.) before running multiple backend workers/processes, since LocMemCache is per-process.
- OAuth-only users have no way to set a password yet (would need a "set password" flow).
- `docs/AUTH.md` is referenced by the disabled-provider error message (`accounts/api.py`, `accounts/oauth.py` callers) and now exists — explains how to configure GitHub/Google OAuth app credentials.
- Backend admin site is enabled at `/admin` with no additional hardening (rate limiting, 2FA) — fine for a hackathon, revisit before any real production use.
- `scripts/test-scripts.sh`'s "don't kill an unrelated process without --force" case could not be verified in this sandboxed dev environment: `$!` from a backgrounded process and the PID `lsof`/`ss` report as actually owning the socket diverge here (confirmed manually), which looks like a sandbox process-wrapping artifact rather than a bug in `ensure_port_free`. Re-run `scripts/test-scripts.sh` on a normal machine to confirm.
