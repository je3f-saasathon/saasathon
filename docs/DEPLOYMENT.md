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
   on a different account. Set its public hostnames: `buggly.dev`
   -> frontend port, `api.buggly.dev` -> backend port (keep hostnames one
   level deep — Cloudflare's free Universal SSL only covers one level of
   subdomain, so a second-level subdomain like `api.staging.buggly.dev`
   fails TLS; use a hyphenated single-level name like `api-staging` instead).
4. Run the connector as a systemd service on the prod host using that
   tunnel's install command from the dashboard (`cloudflared service
   install <token>`), so it survives reboot. This is a separate cloudflared
   instance/service from any other project's tunnel on the same host.

## SRE agent in production

`docker-compose.prod.yml` runs the SRE agent next to the backend:
- `temporal`: a single-node Temporal server (`start-dev`, persisted to the `temporal_data_prod` volume). It's only reachable on the compose network, and no host port is published. Inspect it with `docker compose -f docker-compose.prod.yml exec temporal temporal workflow list`. For more than one box, move to a real Temporal cluster. The client doesn't do TLS or API keys yet, so Temporal Cloud needs a code change.
- `sre-worker`: the Temporal worker (`python -m sre.worker`), built from the backend image.
- `sre-docker`: a privileged `docker:dind` sidecar that the worker starts sandbox containers on (`DOCKER_HOST=tcp://sre-docker:2375`, reachable only on the compose network). Sandboxes never run on the host's Docker.

`deploy-backend.yml` handles all three. After the backend's health check passes, it runs `up -d --build temporal sre-docker sre-worker` and rebuilds the sandbox image inside `sre-docker` (layers are cached, so it's quick). To build the sandbox image by hand on the host, run `ENV_FILE_DIR=~/prod-saasathon make sandbox-image-prod`.

**One-time setup on the prod host**
1. Put the GitHub App's private key at `$ENV_FILE_DIR/secrets/github-app.pem`. That folder is mounted read-only at `/secrets` into `backend` (it lists repos when a project is created) and `sre-worker` (it clones, pushes and opens PRs).
2. Add to `$ENV_FILE_DIR/backend/.env.prod`:
   ```bash
   TEMPORAL_ADDRESS=temporal:7233
   TEMPORAL_NAMESPACE=default
   TEMPORAL_TASK_QUEUE=sre-pipeline
   # Encrypts users' LLM API keys. Back it up: losing it makes stored keys
   # unreadable, and rotating it needs a re-encryption.
   SRE_FIELD_ENCRYPTION_KEY=   # python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   GITHUB_APP_ID=
   GITHUB_APP_PRIVATE_KEY_PATH=/secrets/github-app.pem
   GITHUB_APP_SLUG=            # Connect GitHub, see docs/AUTH.md
   GITHUB_APP_CLIENT_ID=
   GITHUB_APP_CLIENT_SECRET=
   GITHUB_APP_WEBHOOK_SECRET=  # approves draft-only runs on PR merge, see docs/AUTH.md
   SRE_ALLOW_PRIVATE_LLM_URLS=false   # otherwise users can point LLM configs at internal services
   SRE_SANDBOX_NETWORK=none
   # Network for the dependency-install step only (uv sync / pip / npm ci). The worker
   # disconnects the sandbox and checks it's offline before the agent runs. none = no installs.
   SRE_SANDBOX_INSTALL_NETWORK=bridge
   # Optional tracing. Blank = off. Token counts on the dashboard come from the DB either way.
   LANGFUSE_HOST=
   LANGFUSE_PUBLIC_KEY=
   LANGFUSE_SECRET_KEY=
   ```
3. On the GitHub App (permissions: Contents read/write, Pull requests read/write), add the Callback URL `https://api.buggly.dev/api/sre/github/callback` and follow the rest of `docs/AUTH.md` ("GitHub App (SRE agent)").
4. Re-run the deploy (`gh workflow run deploy-backend.yml`) or push to `main`.

Roll back the worker the same way as the backend, since it ships from the same image. In-flight workflows resume on the new worker. If a deploy changes the workflow's control flow, drain running workflows first, because Temporal requires deterministic replay.
