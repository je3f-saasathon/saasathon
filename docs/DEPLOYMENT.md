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
  `~/prod-Je3f-saasathon/{backend,frontend}/.env.prod`, override with `ENV_FILE_DIR`),
  never committed.

## One-time manual infra setup
1. Register two self-hosted Actions runners on the prod host, labels
   `[self-hosted, je3f-saasathon-oma, backend]` and `..., frontend]`.
2. Create `.env.prod` files at the `ENV_FILE_DIR` path for `backend/` and `frontend/`.
3. Configure Cloudflare Tunnel ingress: `dev.andrewplescan.com` -> frontend port,
   `api.dev.andrewplescan.com` -> backend port, then `sudo systemctl restart cloudflared`.
