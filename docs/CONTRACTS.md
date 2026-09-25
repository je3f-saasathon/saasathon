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

## SRE agent

An LLM SRE pipeline that runs as a Temporal workflow (`backend/sre/`). Uptrace calls the webhook with a trace/exception id; the workflow double-checks the anomaly, classifies the bug, finds or creates a playbook, and (depending on execution mode) writes a diagnosis, drafts a fix branch for approval, or opens a PR.

All routes are under `/api/sre` and require `Authorization: Bearer <token>`, except the webhook.

**Access.** A project's members have a role: `owner` > `admin` > `viewer`. Not a member → `404` (the project's existence isn't revealed). Member without the needed role → `403`. Reads need `viewer`; approvals, playbook edits, execution mode and LLM config changes need `admin`; members, repo wiring, webhook secret and deletion need `owner`. A project always keeps at least one owner (`409` otherwise).

**Enums.**
- `execution_mode`: `autonomous` (opens the PR itself) · `draft_only` (default; opens a GitHub **draft** PR right away, and an admin's approval marks it ready for review while rejection closes it. On repos whose plan doesn't allow drafts it opens a normal PR titled `[Awaiting approval] …` instead, and approval removes the prefix) · `advisory_only` (writes a diagnosis only, never touches the repo). An `unconfirmed` playbook never runs above `draft_only`.
- `provider`: `anthropic` · `openai` · `self_hosted` (any OpenAI-compatible server; needs `base_url`) · `jev_cloudflare` (only for `anomaly_double_check`, `bug_classification`, `playbook_similarity_judge`).
- `step`: `anomaly_double_check` · `bug_classification` · `playbook_similarity_judge` · `playbook_creation` · `playbook_execution`.
- incident `status`: `running` · `no_anomaly` · `new_playbook_created` · `awaiting_approval` · `succeeded` · `failed` · `advisory_complete`.
- playbook `status`: `unconfirmed` · `confirmed` · `failing` (3 failed runs in a row; excluded from matching).
- playbook run `status`: `running` · `pending_approval` · `rejected` · `succeeded` · `failed`.

### Webhook (called by Uptrace)

`POST /sre/webhooks/uptrace/{project_id}` — no bearer token. Authenticate with **one** of:
- `X-SRE-Signature: sha256=<hex HMAC-SHA256 of the raw request body, keyed with the project's webhook secret>` (preferred)
- `X-SRE-Webhook-Secret: <the project's webhook secret>` (for senders that can't sign)

Body: `{trace_id: string, exception_id?: string, source_id?: string, payload?: object}` — configure Uptrace's webhook template to send this shape; `payload` is stored verbatim and shown to the LLM as untrusted data.

Returns `200 {incident_run_id, temporal_workflow_id, status}`. Idempotent per `(project, trace_id)`: repeats return the same run and never re-run the pipeline. `401 {detail}` for a bad signature or unknown project (same answer for both). `503 {detail}` if Temporal is unreachable — safe to retry.

### Projects & members

| Method | Path | Role | Body / Params | Returns |
|---|---|---|---|---|
| GET | `/sre/projects` | member | — | `Project[]` |
| POST | `/sre/projects` | any user | `{name, github_installation_id, github_repo_owner, github_repo_name, github_default_branch?, uptrace_source_id?, default_execution_mode?}` | `201 Project & {webhook_secret, webhook_url}` — the only time the secret is shown besides rotation. Caller becomes owner. `400` unless the caller has connected that installation (see GitHub connect) and it can reach the repo. |
| GET | `/sre/projects/{id}` | viewer | — | `Project` |
| PATCH | `/sre/projects/{id}` | admin (owner for `github_*` / `uptrace_source_id`) | any subset of the create fields, plus `default_llm_config_id: int\|null` (must be one of the caller's own configs) | `Project`; `400` if the installation/repo *changes* and fails the same check as create (unchanged wiring is grandfathered) |
| DELETE | `/sre/projects/{id}` | owner | — | `204` |
| POST | `/sre/projects/{id}/webhook-secret/rotate` | owner | — | `{webhook_secret, webhook_url}` |
| GET | `/sre/projects/{id}/members` | viewer | — | `{user_id, email, name, role}[]` |
| POST | `/sre/projects/{id}/members` | owner | `{email, role}` (user must already have an account) | `201 Member`, `404` unknown email, `409` already a member |
| PATCH | `/sre/projects/{id}/members/{user_id}` | owner | `{role}` | `Member`, `409` if it would remove the last owner |
| DELETE | `/sre/projects/{id}/members/{user_id}` | owner, or the member themself | — | `204`, `409` last owner. The member's LLM configs are detached from the project. |

`Project`: `{id, name, role, github_installation_id, github_repo_owner, github_repo_name, github_default_branch, uptrace_source_id, default_execution_mode, default_llm_config_id, created_at, github_verified}` (`role` is the caller's; `github_verified` is true when an owner has connected the project's installation).

### GitHub connect (prove access to a GitHub App installation)

| Method | Path | Auth | Body / Params | Returns |
|---|---|---|---|---|
| GET | `/sre/github/status` | user | — | `{configured, app_slug}` — `configured` is false without `GITHUB_APP_SLUG` / `_CLIENT_ID` / `_CLIENT_SECRET` |
| POST | `/sre/github/connect` | user | — | `{install_url, authorize_url}`, each carrying a signed 10-minute `state` for the caller; `400` if not configured |
| GET | `/sre/github/callback` | none (`state`) | `?code&state` (GitHub adds `installation_id`, `setup_action`) | `302` to `{FRONTEND_URL}/settings?tab=github&github=connected&count=N`, or `…&github_error=invalid_state\|state_expired\|authorization_missing\|token_exchange_failed\|github_api_failed\|not_configured`. On success it replaces the caller's installation list with what `GET /user/installations` returns for this App. |
| GET | `/sre/github/installations` | user | — | `GitHubInstallation[]` (the caller's own) |
| DELETE | `/sre/github/installations/{id}` | user (own) | — | `204` — unlinks our record only; nothing changes on GitHub |
| GET | `/sre/github/installations/{id}/repos` | user (own) | — | `[{owner, name, default_branch, private}]`; `502` if GitHub fails |

`GitHubInstallation`: `{id, installation_id, account_login, account_type}`. `id` is ours; `installation_id` is GitHub's (what projects store).

### LLM configs (owned by a user, attached to projects by reference)

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/sre/llm-configs` | — | caller's `LLMConfig[]` |
| POST | `/sre/llm-configs` | `{name, provider, model?, base_url?, api_key?, extra_config?}` | `201 LLMConfig`; `400` if `base_url` isn't public https (unless `SRE_ALLOW_PRIVATE_LLM_URLS=true`) |
| PATCH | `/sre/llm-configs/{id}` | any subset; `api_key: ""` clears the key | `LLMConfig` |
| DELETE | `/sre/llm-configs/{id}` | — | `204` (projects using it fall back / fail fast) |
| GET | `/sre/projects/{id}/step-overrides` | viewer | `{step, llm_config_id, llm_config_name}[]` |
| PUT | `/sre/projects/{id}/step-overrides` | admin; `{overrides: {[step]: llm_config_id \| null}}` — null removes, unlisted steps unchanged; configs must be the caller's own | `StepOverride[]`; `400` for Jev on a step it can't run |

`LLMConfig`: `{id, name, provider, model, base_url, has_api_key, extra_config, created_at}`. The API key is encrypted at rest and **never** returned. A step uses its override if set, else the project's default config; with neither, the incident fails with a clear `error_message`.

### Playbooks

| Method | Path | Role | Body / Params | Returns |
|---|---|---|---|---|
| GET | `/sre/projects/{id}/playbooks` | viewer | `?status=` | `{playbooks: Playbook[], total}` |
| POST | `/sre/projects/{id}/playbooks` | admin | `{title, description?, keywords?, steps?, execution_mode_override?}` | `201 Playbook` (human-written → `confirmed`) |
| GET | `/sre/playbooks/{id}` | viewer | — | `Playbook` |
| PATCH | `/sre/playbooks/{id}` | admin | any of `title, description, keywords, steps, status, execution_mode_override` (null clears the override) | `Playbook`; setting a non-`failing` status resets the failure count |
| DELETE | `/sre/playbooks/{id}` | admin | — | `204` |

`Playbook`: `{id, project_id, title, description, keywords: string[], steps: Step[], status, execution_mode_override, consecutive_failure_count, source_incident_run_id, created_at, updated_at}`. `Step` is `{type: "edit_file", path, instructions}` or `{type: "run_command", command}`; any other step type is dropped on save (a playbook can't open PRs or push).

### Incident runs & approvals

| Method | Path | Role | Body / Params | Returns |
|---|---|---|---|---|
| GET | `/sre/incident-runs` | member (across all your projects) | `?project_id=&status=&page=1&page_size=20` | `{runs: IncidentRun[], total}` |
| GET | `/sre/incident-runs/{id}` | viewer | — | `IncidentRun` |
| GET | `/sre/playbook-runs/{id}` | viewer | — | `PlaybookRun` |
| POST | `/sre/playbook-runs/{id}/approve` | admin | `{approve: bool}` — `true` marks the PR ready for review, `false` closes it | `PlaybookRun`; `409` if not `pending_approval` or already decided; `503` if the workflow can't be reached (decision released, retry) |

`IncidentRun`: `{id, project_id, trace_id, uptrace_exception_id, temporal_workflow_id, status, classification, matched_playbook_id, created_playbook_id, playbook_run_id, diagnosis_report, error_message, created_at, updated_at, project_name, playbook, pr_url, playbook_run_status, execution_mode, usage}`. It backs the frontend `/dashboard` table.
- `playbook`: `{id, title, status, source}` or `null` — the matched playbook (`source: "matched"`), else the one written from this incident (`"created"`).
- `pr_url` is `""` when no PR was opened; `playbook_run_status` / `execution_mode` are `null` without a playbook run.
- `usage`: `{calls, input_tokens, output_tokens, total_tokens, models: string[], by_step: [{step, provider, model, calls, input_tokens, output_tokens}]}` — one entry per LLM call the incident made (a retried activity counts again; those tokens were spent). `step` is the pipeline step, or `diagnosis_report`. Jev calls count as calls with 0 tokens.

`PlaybookRun`: `{id, incident_run_id, playbook_id, execution_mode, status, approved_by_id, approved_at, pr_url, branch_name, attempts: Attempt[]}` where `Attempt` is `{attempt_number, outcome, summary, error_output, generated_steps, branch_name, langfuse_trace_id, created_at}` (up to 3; each re-plans from the previous attempt's error). `approved_by_id` / `approved_at` record whoever decided, for approvals and rejections alike.

## Errors

All error responses: `{detail: string}` with the appropriate HTTP status. 401 for missing/invalid/expired token, 403 for wrong owner or insufficient project role, 404 for missing resource (or a project you're not a member of), 409 for state conflicts, 422 for validation errors (django-ninja default).
