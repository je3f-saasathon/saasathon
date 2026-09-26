# Auth setup

## GitHub OAuth App

GitHub Settings > Developer settings > OAuth Apps > New OAuth App.

- Local dev callback: `http://localhost:8000/api/auth/github/callback`
- Production callback: `https://api-dev.andrewplescan.com/api/auth/github/callback`

Set `GITHUB_CLIENT_ID` and `GITHUB_CLIENT_SECRET` in `backend/.env` (or `.env.prod`).

## Google OAuth client

Google Cloud Console > APIs & Services > Credentials > Create Credentials >
OAuth client ID > Application type "Web application".

- Local dev callback: `http://localhost:8000/api/auth/google/callback`
- Production callback: `https://api-dev.andrewplescan.com/api/auth/google/callback`

Set `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` in `backend/.env` (or `.env.prod`).

Both callback URLs work unchanged through `scripts/dev-tunnel.sh`, since it forwards
the same ports back to `localhost`.

## GitHub App (SRE agent)

The SRE agent clones, pushes and opens PRs as a GitHub App (`GITHUB_APP_ID`,
`GITHUB_APP_PRIVATE_KEY_PATH`). A project can only use an installation that one of its
editors has proved access to through **Settings → GitHub → Connect GitHub**, so nobody
can point a project at another account's installation. In the App's settings
(GitHub → Settings → Developer settings → GitHub Apps → your app):

- **Callback URL**: `{backend}/api/sre/github/callback`, e.g.
  `http://localhost:8000/api/sre/github/callback` (native dev), `http://localhost:8300/...`
  (docker dev, also what the tunnel forwards) and `https://api-dev.andrewplescan.com/api/sre/github/callback` (prod).
  A GitHub App can list several callback URLs.
- Tick **Request user authorization (OAuth) during installation**. GitHub then sends the
  user to the callback (with `code` and our `state`) after installing. Leave the separate
  Setup URL empty; GitHub ignores it when this box is ticked.
- **Client secrets → Generate a new client secret.**
- Prod only: **Where can this GitHub App be installed? → Any account**, so customers can install it.
- **Webhook**: tick **Active**, set **Webhook URL** to `{backend}/api/sre/github/webhook` and
  a **Webhook secret** (any long random string). Under **Permissions & events → Subscribe to
  events**, tick **Pull request**. This is how draft-only runs are approved: merging the
  agent's PR approves the fix, closing it unmerged rejects it. GitHub must be able to reach
  the URL, so use the tunnel (or prod) rather than plain `localhost`. GitHub doesn't retry
  failed deliveries; redeliver them from the App's **Advanced** tab.

Then set these in `backend/.env` (or `.env.prod`) and recreate the backend container:

- `GITHUB_APP_SLUG`: the `<slug>` in `https://github.com/apps/<slug>`
- `GITHUB_APP_CLIENT_ID`: the App's Client ID (not the App ID)
- `GITHUB_APP_CLIENT_SECRET`: the secret you just generated
- `GITHUB_APP_WEBHOOK_SECRET`: the webhook secret you set above

The callback asks GitHub (with the user's token, which is never stored) for
`/user/installations` and records the ones for this App, replacing that user's previous list.
Projects created before this flow keep working and show as "Unverified" until an owner
connects GitHub. Changing a project's installation or repo always needs the check.

## Account linking

Accounts are linked by verified email: if a user signs in with GitHub or Google
using an email that already exists (password or another provider), the login is
attached to that existing user rather than creating a duplicate.

## Dev tokens

`make dev-token` mints a bearer token for the demo user via a Django management
command, for local API testing. It refuses to run unless backend `DEBUG=true` and
must never be exposed over HTTP.
