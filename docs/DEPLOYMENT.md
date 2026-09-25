# Deployment

Deploys are CI-driven, triggered once CI (`.github/workflows/ci.yml`) goes green
on `main`. Backend and frontend deploy independently via `deploy-backend.yml` /
`deploy-frontend.yml`.
- **View status**: `gh run list --workflow=deploy-backend.yml`, `gh run watch <run-id>`.
- **Roll back**: re-run `workflow_dispatch` against the prior known-good SHA, or
  `git revert` + push to `main`.
- **Tail prod logs**: on the prod host, from the CI-managed checkout:
  `docker compose -f docker-compose.prod.yml logs -f`.
- **Env vars**: live in `.env.prod` files outside the git checkout (default
  `~/prod-saasathon/{backend,frontend}/.env.prod`, override with `ENV_FILE_DIR`),
  never committed.

## One-time manual infra setup
1. Register two self-hosted Actions runners on the prod host, labels
   `[self-hosted, je3f-saasathon-oma, backend]` and `..., frontend]`.
2. Create `.env.prod` files at the `ENV_FILE_DIR` path for `backend/` and `frontend/`.
3. Create a Cloudflare Tunnel dedicated to this project (Zero Trust dashboard
   → Networks → Tunnels → Create). It must live on the same Cloudflare
   account that owns the app's domain zone — a tunnel can't route to a zone
   on a different account. Set its public hostnames: `dev.andrewplescan.com`
   -> frontend port, `api-dev.andrewplescan.com` -> backend port (note: no
   `api.dev...` — Cloudflare's free Universal SSL only covers one level of
   subdomain, so a second-level subdomain like `api.dev.andrewplescan.com`
   fails TLS; use a hyphenated single-level name instead).
4. Run the connector as a systemd service on the prod host using that
   tunnel's install command from the dashboard (`cloudflared service
   install <token>`), so it survives reboot. This is a separate cloudflared
   instance/service from any other project's tunnel on the same host.

## SRE agent in production

`docker-compose.prod.yml` adds two services:
- `sre-worker` — the Temporal worker (`python -m sre.worker`), same image as the backend.
- `sre-docker` — a privileged `docker:dind` sidecar the worker starts sandbox containers on (`DOCKER_HOST=tcp://sre-docker:2375`, reachable only on the compose network). Sandboxes never run on the host's Docker. After the first deploy, and whenever `backend/sre/sandbox/Dockerfile` changes, run `make sandbox-image-prod` on the prod host.

Temporal and Langfuse are **not** part of the prod compose file. Point the backend at them in `backend/.env.prod`:
- `TEMPORAL_ADDRESS`, `TEMPORAL_NAMESPACE`, `TEMPORAL_TASK_QUEUE` — Temporal Cloud, or a self-hosted cluster (`docker-compose.infra.yml`'s `start-dev` server is fine for a single box but isn't a production Temporal).
- `LANGFUSE_HOST`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` — Langfuse Cloud, or a self-hosted Langfuse. If you self-host with `docker-compose.infra.yml`, override every `CHANGEME` value there through env vars first.

Required in `backend/.env.prod`:
- `SRE_FIELD_ENCRYPTION_KEY` — encrypts users' LLM API keys. Losing it makes the stored keys unreadable; rotating it needs a re-encryption.
- `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY_PATH` — the GitHub App the agent pushes and opens PRs as (permissions: Contents read/write, Pull requests read/write). Mount the key file into the `sre-worker` container.
- `SRE_ALLOW_PRIVATE_LLM_URLS=false` — otherwise users can point LLM configs at internal services.
- `SRE_SANDBOX_NETWORK=none`.

Roll back the worker the same way as the backend (it ships from the same image). In-flight workflows resume on the new worker; if a deploy changes the workflow's control flow, drain running workflows first (Temporal requires deterministic replay).
