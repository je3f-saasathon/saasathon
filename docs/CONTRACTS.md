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
| POST   | `/auth/tokens`               | Bearer token                          | `{token, expires_at}`: a new token for scripts, shown once. Separate from the caller's session (logging out doesn't revoke it); 30-day expiry like any token |
| POST   | `/auth/cli/start`            | —                                      | `{device_code, user_code, verification_url, expires_in, interval}`: starts a CLI login (`buggly login`). `verification_url` is `{FRONTEND_URL}/cli?code={user_code}`; both codes expire after 10 minutes |
| POST   | `/auth/cli/approve`          | Bearer token, `{user_code}`           | `{ok: true}`: the signed-in user approves that CLI login. `404` unknown or expired code, `409` already approved |
| POST   | `/auth/cli/poll`             | `{device_code}`                       | `200 {token, user}` once approved (only once; the login is then used up), `202 {status: "pending"}` until then, `410 {detail}` unknown or expired. Poll every `interval` seconds |
| GET    | `/auth/providers`            | —                                      | `{password: true, github: bool, google: bool}` |

`user` shape: `{id, email, name, avatar_url, role, created_at}`

## Health

| Method | Path           | Returns                    |
|--------|-----------------|------------------------------|
| GET    | `/health`       | `{status: "ok", version}`   |

## Jev (Cloudflare Workers AI, experimental)

Requires `Authorization: Bearer <token>`. Requires `CLOUDFLARE_ACCOUNT_ID` / `CLOUDFLARE_API_TOKEN` to be set on the backend, and gateway balance (or BYOK) on the Cloudflare account — otherwise upstream calls fail and this returns 502.

| Method | Path        | Body / Params                                              | Returns                              |
|--------|-------------|--------------------------------------------------------------|----------------------------------------|
| POST   | `/jev/run`  | `{state: string, questions: {[name]: {type: "noul"\|"choice"\|"score", instructions, criteria}}}` | `{model, answers, usage}` or `502 {detail}` |

## SRE agent

An LLM SRE pipeline that runs as a Temporal workflow (`backend/sre/`). Uptrace calls the webhook with a trace/exception id; the workflow double-checks the anomaly, classifies the bug, finds or creates a playbook, and (depending on execution mode) writes a diagnosis, drafts a fix branch for approval, or opens a PR.

All routes are under `/api/sre` and require `Authorization: Bearer <token>`, except the webhook.

**Access.** A project's members have a role: `owner` > `admin` > `viewer`. Not a member → `404` (the project's existence isn't revealed). Member without the needed role → `403`. Reads need `viewer`; approvals, playbook edits, execution mode and LLM config changes need `admin`; members, repo wiring, webhook secret and deletion need `owner`. A project always keeps at least one owner (`409` otherwise).

**Organizations.** Every project belongs to an organization, and every user has a **personal** organization, created with the account (email sign-up and both OAuth logins). An org's members have an org role: `owner` > `admin` > `member`. Org roles cover org-level things (members, Uptrace credentials, org playbooks, moving projects into the org); project roles still decide project access. Being a project member does **not** make you an org member. Not an org member → `404`; too low an org role → `403`.

**Playbooks vs runbooks** (when the server has `SRE_RUNBOOKS_ENABLED=true`; otherwise playbooks are project-scoped as they always were, and there are no runbooks). A **playbook** is generic: how to handle a *class* of bug (e.g. a null dereference in a request handler). It's either a **built-in** that we ship (`organization_id: null`, read-only, visible to every project) or belongs to an organization and is visible to all of that org's projects. A **runbook** is specific: the files and commands that fixed a bug in one project's repo, linked to the playbook it came from. An incident first looks for a runbook that fits, then a playbook. A successful fix from a playbook saves a new runbook.

**Enums.**
- `execution_mode`: `autonomous` (opens the PR itself) · `draft_only` (default; opens a GitHub **draft** PR right away, and an admin's approval marks it ready for review while rejection closes it. On repos whose plan doesn't allow drafts it opens a normal PR titled `[Awaiting approval] …` instead, and approval removes the prefix) · `advisory_only` (writes a diagnosis only, never touches the repo). An `unconfirmed` playbook never runs above `draft_only`. With runbooks on, `autonomous` needs a `confirmed` **runbook**: a run from a playbook alone (even a confirmed built-in), or from an unconfirmed runbook, is capped at `draft_only`.
- `provider`: `anthropic` · `openai` · `self_hosted` (any OpenAI-compatible server; needs `base_url`) · `jev_cloudflare` (only for `anomaly_double_check`, `bug_classification`, `playbook_similarity_judge`).
- `step`: `anomaly_double_check` · `bug_classification` · `playbook_similarity_judge` · `playbook_creation` · `playbook_execution` · `repository_scan` (remediation agents' scanner).
- incident `status`: `running` · `no_anomaly` · `new_playbook_created` · `awaiting_approval` · `succeeded` · `failed` · `rejected` (a draft-only fix whose PR was closed unmerged; not a failure) · `advisory_complete` · `delegated` (service mesh: the root cause is in another project's service, and a linked child incident there took over).
- incident `source`: `alert` (an Uptrace alert or a direct `trace_id` call) · `linked` (a child created by the service mesh for the culprit's project) · `scan` (a remediation agent's finding).
- agent `kind`: `playbook_sweep` (default) · `runbook_variant` · `find_quiet`. agent `trigger`: `on_merge` (default) · `branch_watch` · `schedule`. A manual run is `manual` on the scan run.
- scan run `status`: `running` · `succeeded` · `partial` (some repos failed) · `failed`.
- playbook / runbook `status`: `unconfirmed` · `confirmed` · `failing` (3 failed runs in a row; excluded from matching). A run counts toward its runbook's streak when it used one, else toward the playbook's. Built-in playbooks' status never changes from runs.
- playbook `origin`: `builtin` · `agent` · `human`. runbook `origin`: `agent` · `human` · `migrated` (converted from an old, specific playbook).
- playbook `category` (and the classifier's): `null_reference` · `timeout` · `database` · `dependency_failure` · `configuration` · `validation` · `resource_exhaustion` · `logic_error` · `other`.
- org `role`: `owner` · `admin` · `member`.
- playbook run `status`: `running` · `pending_approval` · `rejected` · `succeeded` · `failed`.

### Webhook (called by Uptrace)

`POST /sre/webhooks/uptrace/{project_id}` — no bearer token. Authenticate with **one** of:
- `?token=<the project's webhook secret>` in the URL. This is the only option for **Uptrace**, whose webhook channel takes just a URL: no custom headers and no signing. Project create and secret rotation return it ready-made as `uptrace_webhook_url`.
- `X-SRE-Signature: sha256=<hex HMAC-SHA256 of the raw request body, keyed with the project's webhook secret>`, for senders that can sign.
- `X-SRE-Webhook-Secret: <the project's webhook secret>`, for other senders that can't sign.

Body, either of:
- **Uptrace's alert notification, as Uptrace sends it**: `{id, eventName, payload, createdAt, alert: {id, url, name, type, state, status, createdAt}}` (`payload` may be `null`). It has no trace id; `alert.name` carries the error text. Only open alerts (`status`, else `state`, is `unresolved`, or `open` on older Uptrace) with `eventName` `created`, `recurring`, `status_changed` or `state_changed` (hyphenated forms also accepted) start work. Anything else returns `202 {detail}` and does nothing: an alert resolving, or the `test` message Uptrace sends when a webhook channel is saved. The incident key is `uptrace-alert-{alert.id}`, so an error that keeps recurring is **one** incident, because Uptrace already groups repeats into one alert. **Reopens:** when the alert comes back (`status_changed` / `state_changed` to open) after its latest incident finished (anything but `running` / `awaiting_approval`), a new incident starts with key `uptrace-alert-{alert.id}-r{n}` (n = 2, 3, …). A reopen while an incident is still active, a `recurring` event, or a redelivery maps to the latest incident. **Pinning:** the first alert pins the project to its Uptrace instance and project, read from `alert.url` (`<host>/alerting/<id>/…`) and stored in `uptrace_source_id`, e.g. `app.uptrace.dev/1`. After that, alerts from any other Uptrace project return `409 {detail}`, so a misattached channel can't send the agent after the wrong repo. An owner can clear `uptrace_source_id` to re-pin.
- A direct call: `{trace_id: string, exception_id?: string, source_id?: string, payload?: object}`. The incident key is `trace_id`.

The whole body is stored verbatim and shown to the LLM as untrusted data. With `SRE_UPTRACE_FETCH_ENABLED=true` and an Uptrace credential for the project, the worker also fetches the alert's exception (type, message, stack, `service.name`) from the Uptrace API before triage, and shows that to the LLM too. A failed fetch never fails the incident.

Returns `200 {incident_run_id, temporal_workflow_id, status}`. Idempotent per `(project, incident key)`: repeats return the same run and never re-run the pipeline. `401 {detail}` for a bad secret or unknown project (same answer for both). `409 {detail}` for an alert from an Uptrace project this one isn't pinned to. `422 {detail}` if the body has neither an alert nor a `trace_id`. `503 {detail}` if Temporal is unreachable, which is safe to retry.

### Projects & members

| Method | Path | Role | Body / Params | Returns |
|---|---|---|---|---|
| GET | `/sre/projects` | member | — | `Project[]` |
| POST | `/sre/projects` | any user | `{name, github_installation_id, github_repo_owner, github_repo_name, github_default_branch?, uptrace_source_id?, default_execution_mode?, generate_tests?, organization_id?, service_names?, uptrace_managed?, uptrace_share_with_project_id?, platform_preset?}` | `201 Project & {webhook_secret, webhook_url, uptrace_webhook_url}` — the only time the secret is shown besides rotation. Caller becomes owner. `400` unless the caller has connected that installation (see GitHub connect) and it can reach the repo. `organization_id` defaults to the caller's personal org; another org needs the caller to be its `admin` (`403` otherwise, `404` if not a member). |
| GET | `/sre/projects/{id}` | viewer | — | `Project` |
| PATCH | `/sre/projects/{id}` | admin (owner for `github_*` / `uptrace_source_id` / `organization_id` / `uptrace_credential_id`) | any subset of the create fields, plus `default_llm_config_id: int\|null` (must be one of the caller's own configs) and `uptrace_credential_id: int\|null` (must belong to the project's org, `400` otherwise; null = pick the org's credential by host) | `Project`; `400` if the installation/repo *changes* and fails the same check as create (unchanged wiring is grandfathered). Moving to another org needs `admin` in the target org. |
| DELETE | `/sre/projects/{id}` | owner | — | `204`; first cancels the project's running work (see "Deleting a project" below). `503` if Temporal can't be reached while something is still running: nothing is deleted, and a retry is safe |
| POST | `/sre/projects/{id}/webhook-secret/rotate` | owner | — | `{webhook_secret, webhook_url, uptrace_webhook_url}`; a managed project's Uptrace channel is updated to the new URL automatically |
| POST | `/sre/projects/{id}/uptrace/setup` | owner | `{share_with_project_id?: int\|null}` | `Project`. Hands the project's Uptrace to the platform (managed Uptrace), moves it between its own Uptrace project and a shared one, or retries a failed setup. `400` when managed Uptrace is off, or for an invalid share (see below) |
| POST | `/sre/projects/{id}/uptrace/resolve-alerts` | admin | — | `{resolved: n}`: resolves the project's open alerts in its managed Uptrace project (only its own monitor's), so the next occurrence reopens them and starts a new incident. For testing tools. `400` if not managed, `502` if Uptrace fails |
| GET | `/sre/uptrace/managed` | any user | — | `{enabled, url}`: whether this server sets up Uptrace for projects, and the Uptrace URL apps export to |
| GET | `/sre/projects/{id}/members` | viewer | — | `{user_id, email, name, role}[]` |
| POST | `/sre/projects/{id}/members` | owner | `{email, role}` (user must already have an account) | `201 Member`, `404` unknown email, `409` already a member |
| PATCH | `/sre/projects/{id}/members/{user_id}` | owner | `{role}` | `Member`, `409` if it would remove the last owner |
| DELETE | `/sre/projects/{id}/members/{user_id}` | owner, or the member themself | — | `204`, `409` last owner. The member's LLM configs are detached from the project. |

`Project`: `{id, name, role, github_installation_id, github_repo_owner, github_repo_name, github_default_branch, uptrace_source_id, default_execution_mode, default_llm_config_id, generate_tests, platform_preset, platform_tokens_this_month, created_at, github_verified, organization_id, organization_name, uptrace_credential_id, uptrace_fetch_ready, service_names}` (`role` is the caller's; `github_verified` is true when an owner has connected the project's installation). `uptrace_fetch_ready` is true when an Uptrace credential resolves for the project: its own `uptrace_credential_id`, else the single org credential whose `host` matches the host in `uptrace_source_id`. `generate_tests` (default `true`, admin-editable): when `false` the fix agent writes no new tests and only runs the repo's existing ones, a cheaper run. It's frozen onto each playbook run when the run starts. `service_names: string[]` (default `[]`, admin-editable; each 1–255 chars, at most 50): the Uptrace `service.name`s this repo runs, used by the service mesh when a service doesn't send `vcs.repository.url.full`. A name may belong to only one project per organization (`409` otherwise).

**Managed Uptrace** (`UPTRACE_MANAGED_URL` + `UPTRACE_MANAGED_TOKEN` set on the server): users never open Uptrace. A new project (unless it's created with `uptrace_managed: false`, "use my own Uptrace", or an explicit `uptrace_source_id`) gets `uptrace_managed: true` and, in the background (a Temporal workflow with retries), an Uptrace project of its own, pinned as `uptrace_source_id`; an error monitor `platform: project {id}` on exception events; and a webhook channel that only that monitor notifies, pointing at the project's webhook URL. The alert-to-incident path is then the usual webhook. The telemetry fetch and service mesh use the platform's token, so managed projects need no Uptrace credential (`uptrace_fetch_ready` is true once pinned). Project fields: `uptrace_managed`, `uptrace_status` (`""` not managed · `provisioning` · `ready` · `error`), `uptrace_error` (why, for `error`), `uptrace_project_id`, `uptrace_dsn` (where the apps export telemetry, over OTLP/HTTP with the `uptrace-dsn` header; `""` below admin), and `uptrace_shared_with: {id, name}[]`.
- **Sharing:** `uptrace_share_with_project_id` (create) or `share_with_project_id` (setup) puts the project in another managed project's Uptrace project, so calls between their services form one trace for the service mesh. The other project must be in the same org and the caller its admin (`403` otherwise), and both projects need `service_names` (`400`): once shared, each project's monitor only covers its own service names. A shared project can't clear its `service_names` or change organization (`400`); changing its names re-syncs the group.
- The platform owns a managed project's pin: PATCHing `uptrace_source_id` to another value is `400`. Deleting a project removes its monitor and channel (Uptrace has no API to delete the Uptrace project itself).

**Deleting a project** cancels what is still running for it in Temporal *before* deleting anything, so no workflow keeps calling the LLM or pushing branches for a project that is gone:
- Its incidents that haven't finished (`running`, `awaiting_approval`, and `rejected`, whose workflow watches for a reopened PR for 30 days) are cancelled (Temporal cancel, falling back to terminate if the cancel request fails; a workflow that already finished is fine). A cancelled workflow ends as *cancelled* and writes nothing more.
- A running remediation scan run whose only repo is this project is cancelled and marked `failed` ("Cancelled: its only project was deleted"). A running scan run that also covers other projects keeps going: this project's repo entry is removed and the run finishes over the others.
- The project is removed from every agent's `project_ids`. An agent whose list was only this project is also disabled (and its schedule removed), because an empty list means every project in the org.
- If Temporal is unreachable and any of the above is needed, the request is `503` and nothing changes. With nothing running, Temporal isn't contacted.
- Then the project is deleted with its members, step overrides, incidents (and their playbook runs, attempts and LLM usage rows), runbooks and scan repo entries. Its legacy (project-only, non-generic) playbooks are deleted too, since nothing else could see them; its org's generic playbooks stay (their origin project is cleared).
- Linked incidents: a child incident this project delegated to another project keeps running there (its `parent_incident_run_id` becomes `null`); this project's own linked children of another project's incident are cancelled and deleted with it.
- Pull requests on GitHub (draft or ready) are **left open**: they live in the user's repo and may still be worth merging. Merging or closing one afterwards is ignored by the GitHub webhook.
- For a managed-Uptrace project, its monitor and channel are removed afterwards (see "Managed Uptrace" above).

### Organizations

| Method | Path | Role | Body / Params | Returns |
|---|---|---|---|---|
| GET | `/sre/organizations` | any user | — | `Organization[]` (the caller's, personal one included) |
| POST | `/sre/organizations` | any user | `{name}` | `201 Organization`; caller becomes `owner` |
| GET | `/sre/organizations/{id}` | member | — | `Organization` |
| PATCH | `/sre/organizations/{id}` | owner | `{name}` | `Organization` |
| GET | `/sre/organizations/{id}/members` | member | — | `{user_id, email, name, role}[]` |
| POST | `/sre/organizations/{id}/members` | owner | `{email, role}` (user must already have an account) | `201 OrgMember`, `404` unknown email, `409` already a member, `400` on a personal org |
| PATCH | `/sre/organizations/{id}/members/{user_id}` | owner | `{role}` | `OrgMember`, `409` if it would remove the last owner |
| DELETE | `/sre/organizations/{id}/members/{user_id}` | owner, or the member themself | — | `204`, `409` last owner |

`Organization`: `{id, name, is_personal, role, created_at}` (`role` is the caller's). A personal org has exactly one member, its owner, and can't take members.

### Uptrace credentials (per organization)

An Uptrace API token is user-scoped (it reaches every Uptrace project that user can see), so it's stored once per org and projects use it by reference. The token is encrypted at rest and **never** returned.

| Method | Path | Role | Body | Returns |
|---|---|---|---|---|
| GET | `/sre/organizations/{id}/uptrace-credentials` | org admin | — | `UptraceCredential[]` |
| POST | `/sre/organizations/{id}/uptrace-credentials` | org admin | `{name, host, api_base_url, token}` | `201 UptraceCredential`; `400` if `api_base_url` isn't public https (unless `SRE_ALLOW_PRIVATE_UPTRACE_URLS=true`), `409` duplicate name |
| PATCH | `/sre/uptrace-credentials/{id}` | org admin | any subset; a new `token` rotates it | `UptraceCredential` |
| DELETE | `/sre/uptrace-credentials/{id}` | org admin | — | `204` (projects using it fall back to host matching) |

`UptraceCredential`: `{id, organization_id, name, host, api_base_url, has_token, created_by_id, created_at, updated_at}`. `host` is the Uptrace host that appears in alert links and `uptrace_source_id` (e.g. `app.uptrace.dev`); `api_base_url` is where its API lives. The worker only calls it for the project's pinned Uptrace project, and never with a credential whose `host` differs from the pinned one.

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

**Projects on install** (with `SRE_GITHUB_AUTO_PROJECTS=true`): connecting an installation creates a project for each repo it can reach, so installing the App is the whole setup. Each gets: the repo's name and default branch, the caller's personal org, the caller as owner, `default_execution_mode: draft_only`, `service_names: [repo name]` (unless the org already uses that name), and managed Uptrace when it's configured. It happens in two places: `GET /sre/github/callback`, for installations the caller hadn't connected before (its redirect adds `&projects=N`); and the GitHub webhook's `installation` (`created`) and `installation_repositories` (`added`) events, for the connected user whose GitHub account sent the event, or the only connected user if there's one. A repo that already has a project on that installation is skipped, so deleting an auto-created project doesn't bring it back unless the repo is removed from the App and added again.

### CLI (`buggly run`)

| Method | Path | Auth | Body / Params | Returns |
|---|---|---|---|---|
| GET | `/sre/cli/project` | user (admin) | `?repo=owner/name` (from `git remote`) and optional `&project_id=` | `CliProject`; `404 {detail}` if the caller administers no project for that repo; `409 {detail, projects: [{id, name}]}` if several match and no `project_id` was given |

`CliProject`: `{project_id, name, repo, service_name, uptrace_status, dsn, otlp_endpoint}`. `service_name` is the first of the project's `service_names`, else the repo name. `dsn` and `otlp_endpoint` (the DSN's `scheme://host[:port]`, where OTLP/HTTP goes: `/v1/traces`, `/v1/logs`, `/v1/metrics`) are `""` until managed Uptrace is `ready`; the CLI then runs the command without telemetry and says why.

### LLM configs (owned by a user, attached to projects by reference)

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/sre/llm-configs` | — | caller's `LLMConfig[]` |
| POST | `/sre/llm-configs` | `{name, provider, model?, base_url?, api_key?, extra_config?}` | `201 LLMConfig`; `400` if `base_url` isn't public https (unless `SRE_ALLOW_PRIVATE_LLM_URLS=true`) |
| PATCH | `/sre/llm-configs/{id}` | any subset; `api_key: ""` clears the key | `LLMConfig` |
| DELETE | `/sre/llm-configs/{id}` | — | `204` (projects using it fall back / fail fast) |
| GET | `/sre/projects/{id}/step-overrides` | viewer | `{step, llm_config_id, llm_config_name}[]` |
| PUT | `/sre/projects/{id}/step-overrides` | admin; `{overrides: {[step]: llm_config_id \| null}}` — null removes, unlisted steps unchanged; configs must be the caller's own | `StepOverride[]`; `400` for Jev on a step it can't run |

`LLMConfig`: `{id, name, provider, model, base_url, has_api_key, extra_config, created_at}`. The API key is encrypted at rest and **never** returned. `extra_config` accepts `max_tokens` and `temperature`; for `jev_cloudflare` the API key is a Cloudflare API token and `extra_config.account_id` its account id, and an empty key, account id or `model` falls back to the server's `CLOUDFLARE_*` settings. A step uses its override if set, else the project's default config, else the **company default** (see below). With none of those, the incident fails with a clear `error_message`.

### Company default model

| Method | Path | Returns |
|---|---|---|
| GET | `/sre/platform` | `{available, triage_model, strong_model, monthly_token_cap, default_preset, presets: [{key, label, triage_model, strong_model}]}` |

When a project hasn't picked a config for a step, it runs on our keys (server env `SRE_PLATFORM_*`). Triage steps (anomaly check, classification, playbook judge) run on Jev when the server's Cloudflare account is set, reported as `triage_model: "jev"`; otherwise they use the fast model. Playbook writing and running use `strong_model`. `available: false` means there's no company default.
- Each project picks a mix with `Project.platform_preset` (admin-editable; `400` for an unknown key; default `openai_jev`):

  | key | triage steps | playbook writing and running |
  |---|---|---|
  | `openai_jev` | Jev (falls back to the fast model if Cloudflare isn't set) | strong model |
  | `openai` | fast model (`SRE_PLATFORM_FAST_MODEL`) | strong model (`SRE_PLATFORM_STRONG_MODEL`) |
  | `gpt_5_4` | gpt-5.4 | gpt-5.4 |
  | `gpt_5_5` | gpt-5.5 | gpt-5.5 |
- Every call on our credentials counts toward that project's `monthly_token_cap` (per calendar month; `0` = unlimited). This includes a user's Jev config that has no Cloudflare token of its own. Once a project reaches the cap, incidents on our keys fail with an `error_message` telling the user to add their own model. A project's own keys are never capped.
- `Project.platform_tokens_this_month` is its usage this month.

### Playbooks

| Method | Path | Role | Body / Params | Returns |
|---|---|---|---|---|
| GET | `/sre/projects/{id}/playbooks` | viewer | `?status=` | `{playbooks: Playbook[], total}` — with runbooks on: built-ins, the project org's generic playbooks, and this project's legacy (not yet generalised) ones; off: the project's own, as before |
| POST | `/sre/projects/{id}/playbooks` | admin | `{title, description?, keywords?, steps?, execution_mode_override?, category?, symptoms?}` | `201 Playbook` (human-written → `confirmed`); with runbooks on it belongs to the project's org |
| POST | `/sre/organizations/{id}/playbooks` | org admin | same as above | `201 Playbook` |
| GET | `/sre/playbooks/{id}` | viewer on a project that can see it | — | `Playbook` |
| PATCH | `/sre/playbooks/{id}` | admin of a project that can see it, or org admin | any of `title, description, keywords, steps, status, execution_mode_override, category, symptoms` (null clears the override) | `Playbook`; setting a non-`failing` status resets the failure count; `403` on a built-in |
| DELETE | `/sre/playbooks/{id}` | same as PATCH | — | `204`; `403` on a built-in; `409` while runbooks use it |

`Playbook`: `{id, project_id, organization_id, origin, created_by_id, is_generic, title, description, category, symptoms, keywords: string[], steps: Step[], status, execution_mode_override, consecutive_failure_count, source_incident_run_id, created_at, updated_at}`. `project_id` is the project it came from (`null` for built-ins and org-level ones). `is_generic: false` marks a legacy playbook, written for one bug before runbooks existed: it's visible only to its own project until `manage.py generalize_playbooks` rewrites it. The built-ins are one per `category`, and `manage.py seed_playbooks` refreshes them. `Step` is generic — `{type: "investigate" | "change" | "verify", instructions}` — or, for legacy playbooks and runbooks, `{type: "edit_file", path, instructions}` or `{type: "run_command", command}`. Any other step type is dropped on save (nothing can open PRs or push).

### Runbooks (with runbooks on)

| Method | Path | Role | Body / Params | Returns |
|---|---|---|---|---|
| GET | `/sre/projects/{id}/runbooks` | viewer | `?status=&playbook_id=` | `{runbooks: Runbook[], total}` |
| POST | `/sre/projects/{id}/runbooks` | admin | `{playbook_id, title, description?, area?, keywords?, steps?, service_name?}` | `201 Runbook` (human-written → `confirmed`); `400` if the project can't see that playbook |
| GET | `/sre/runbooks/{id}` | viewer | — | `Runbook` |
| PATCH | `/sre/runbooks/{id}` | admin | any of the create fields, plus `status` | `Runbook`; a non-`failing` status resets the failure count |
| DELETE | `/sre/runbooks/{id}` | admin | — | `204` |

With runbooks off, every runbook route returns `404`.

`Runbook`: `{id, project_id, playbook_id, title, description, area, keywords: string[], steps: Step[], status, origin, created_by_id, repo_owner, repo_name, service_name, consecutive_failure_count, source_playbook_run_id, created_at, updated_at}`. Steps use the `edit_file` / `run_command` shapes. `repo_owner` / `repo_name` are the project's repo when the runbook was made; `service_name` is the Uptrace `service.name` it was made for (may be `""`).

### Service mesh (with `SRE_SERVICE_MESH_ENABLED=true`; otherwise these return `404`)

How the org's services call each other, from Uptrace's service graph (`GET /internal/v1/service-graph/{project}`) and from each alert's trace. There's one graph per organization and pinned Uptrace project (`uptrace_source_id`). Refreshed every 15 minutes by the worker; edges not seen for `SRE_SERVICE_GRAPH_TTL_DAYS` (default 7) are dropped. See `docs/MESH_AND_REMEDIATION.md`.

| Method | Path | Role | Body / Params | Returns |
|---|---|---|---|---|
| GET | `/sre/organizations/{id}/service-graph` | org member | `?source=<uptrace_source_id>` (optional when the org's projects use only one) | `ServiceGraph`; `400` if the org's projects use several Uptrace projects and `source` is missing |
| POST | `/sre/organizations/{id}/service-graph/refresh` | org admin | `?source=` (optional; default: every source the org's projects use) | `202 {detail}`: the refresh runs on the worker; `503` if Temporal is unreachable |

`ServiceGraph`: `{organization_id, source, refreshed_at, nodes: ServiceNode[], edges: ServiceEdge[]}`. `refreshed_at` is `null` before the first refresh.

`ServiceNode`: `{id, name, kind, project_id, project_name, mapped_by, repo_url, first_seen_at, last_seen_at}`.
- `kind`: `service` (an instrumented `service.name`) or `system` (a dependency Uptrace names by its system, e.g. `db:postgresql`, or an uninstrumented client such as `<http-client>`).
- `project_id` / `project_name`: the project this service maps to, or `null`. Only projects in this org pinned to this `source` are considered. `mapped_by`: `vcs_attr` (the service's `vcs.repository.url.full` matches the project's GitHub repo), `service_names` (listed on the project), or `""` (unmapped). `repo_url` is the last `vcs.repository.url.full` seen, or `""`.

`ServiceEdge`: `{id, client_id, server_id, type, count, error_count, error_rate, duration_avg_ms, duration_max_ms, rate_per_min, first_seen_at, last_seen_at}`. `client_id` / `server_id` are `ServiceNode.id`s; `type` is Uptrace's edge type (`http`, `db`, `messaging`, `rpc`, …). Counts and durations cover the last refresh window (1 hour).

**Root cause and linked incidents.** With the mesh on, each incident whose telemetry has a `trace_id` walks that trace right after the telemetry fetch (`localize_root_cause`, no LLM). The culprit is the deepest error span with no failing children (the earliest one if several), and it's stored as `IncidentRun.root_cause`. If the culprit's service maps to **another** project in the same org with the same `uptrace_source_id`, a **linked child** incident starts in that project (`source: "linked"`, `parent_incident_run_id` set, telemetry taken from the culprit span, workflow id `sre-incident-{child project}-linked-{parent run id}`). The child runs under its own project's execution mode, playbooks, runbooks and models. The parent ends as `delegated`. Otherwise the incident carries on in its own project, and its diagnosis names the culprit. Nothing about the child is visible to people who aren't members of its project, except `root_cause.project_id` on the parent.

**Neighbour repos.** When a fix runs for a service that has mapped neighbours in the graph (services that call it or that it calls, seen in the last TTL window), up to `SRE_MAX_NEIGHBOUR_REPOS` (default 3, `0` = off) of their repos are cloned **read-only** into the sandbox at `/neighbours/<owner>-<repo>` (default branch, files only, no git metadata). Only neighbours on the **same GitHub App installation** as the project being fixed are used, so the agent never reads, and can't copy into this repo's PR, code from a GitHub account this repo doesn't share. A neighbour that fails to clone is left out. Only the project's own repo is writable, and only it is committed or pushed.

### Remediation agents (with `SRE_REMEDIATION_AGENTS_ENABLED=true`; otherwise these return `404`)

Agents an org admin starts to look for bugs across the org's repos before they alert. Each scan run scans its repos (one activity per repo, at most 2 at once) and turns each finding into an incident with `source: "scan"` that goes through the usual pipeline. See `docs/MESH_AND_REMEDIATION.md`.

| Method | Path | Role | Body / Params | Returns |
|---|---|---|---|---|
| GET | `/sre/organizations/{id}/agents` | org member | — | `RemediationAgent[]` |
| POST | `/sre/organizations/{id}/agents` | org admin | `{name, kind?, trigger?, schedule_cron?, branch_pattern?, project_ids?, playbook_ids?, execution_mode?, max_findings_per_repo?, monthly_token_budget?, enabled?}` | `201 RemediationAgent`; `400` for a project outside the org, a playbook that isn't a built-in or the org's own, `schedule` without a valid 5-field `schedule_cron`, `branch_watch` without `branch_pattern`, or `execution_mode: "autonomous"`; `409` duplicate name; `503` if the agent's Temporal Schedule can't be updated (nothing is saved) |
| GET | `/sre/agents/{id}` | org member | — | `RemediationAgent` |
| PATCH | `/sre/agents/{id}` | org admin | any subset of the create fields | `RemediationAgent`, same `400`/`409`/`503`s. The Schedule follows `trigger`, `schedule_cron` and `enabled`. |
| DELETE | `/sre/agents/{id}` | org admin | — | `204`; its scan runs go too, and the incidents they created stay (`scan_run_id` becomes `null`). `503` if its Schedule can't be removed |
| POST | `/sre/agents/{id}/run` | org admin | — | `202 ScanRun` (trigger `manual`, every covered project's default branch, whole repo); `409` if the agent is disabled or already has a running scan; `503` if Temporal is unreachable (no scan run is kept) |
| GET | `/sre/agents/{id}/scan-runs` | org member | `?page=1&page_size=20` | `{scan_runs: ScanRun[], total}` |
| GET | `/sre/scan-runs/{id}` | org member | — | `ScanRun` |

`RemediationAgent`: `{id, organization_id, name, kind, trigger, schedule_cron, branch_pattern, project_ids, playbook_ids, execution_mode, max_findings_per_repo, monthly_token_budget, tokens_this_month, enabled, created_by_id, created_at, updated_at, last_scan_run_id}`.
- `kind` defaults to `playbook_sweep`, `trigger` to `on_merge`. `project_ids: []` means every project in the org, including ones added later. `playbook_ids: []` means every non-failing playbook each project can see (only `playbook_sweep` reads it).
- `execution_mode`: `advisory_only` or `draft_only`, never `autonomous`. It's a ceiling: a finding's incident runs at the **lower** of this and its evidence cap. `runbook_variant` matches and trace-backed `find_quiet` findings are capped at `draft_only`, everything else at `advisory_only`. Defaults: `draft_only` for `runbook_variant` and `find_quiet`, `advisory_only` for `playbook_sweep`.
- `max_findings_per_repo` default 3 (1–10). `monthly_token_budget` `0` = no limit. Once the scans' tokens this month reach it, new scans fail fast with an `error_message`. The incidents' own tokens still count toward each project's usual cap.
- Triggers: `on_merge`: a PR merged into a covered project's default branch (GitHub App `pull_request` event), scanning only the merged diff. `branch_watch`: a push to a branch matching `branch_pattern` (fnmatch, e.g. `release/*`; GitHub App `push` event), scanning only the pushed diff. `schedule`: `schedule_cron` (UTC) through a Temporal Schedule, scanning the whole repo.

`ScanRun`: `{id, agent_id, trigger, trigger_ref, status, repos: [{project_id, project_name, status, finding_count, incident_run_ids, error}], finding_count, usage, error_message, started_at, finished_at}`. `trigger`: `manual` · `on_merge` · `branch_watch` · `schedule`. `trigger_ref` is the merge/push SHA, the scheduled time, or `""`. `repos[].status`: `pending` · `running` · `succeeded` · `failed` · `skipped` (e.g. the diff didn't touch it). `usage` has the same shape as `IncidentRun.usage` and covers the scanner's calls only.

**Findings.** Each finding becomes an `IncidentRun` with `source: "scan"`, `scan_run_id`, `scan_kind`, and `telemetry` holding the scanner's context, `{exception_type (the category), message, location, evidence, service_name, suggested_playbook_id, suggested_runbook_id, evidence_kind}`. The workflow id is `sre-incident-{project}-scan-{kind}-{sha1(category + location)[:16]}`, so a finding already raised is never raised again by any later scan. `confirm_anomaly` checks the finding and ends it as `no_anomaly` if it doesn't hold up.

### Incident runs & approvals

| Method | Path | Role | Body / Params | Returns |
|---|---|---|---|---|
| GET | `/sre/incident-runs` | member (across all your projects) | `?project_id=&status=&page=1&page_size=20` | `{runs: IncidentRun[], total}` |
| GET | `/sre/incident-runs/{id}` | viewer | — | `IncidentRun` |
| GET | `/sre/playbook-runs/{id}` | viewer | — | `PlaybookRun` |
| POST | `/sre/playbook-runs/{id}/approve` | admin | `{approve: bool}` — `true` marks the PR ready for review, `false` closes it | `PlaybookRun`; `409` if not `pending_approval` or already decided; `503` if the workflow can't be reached (decision released, retry). Fallback only: draft-only runs are normally decided on GitHub (below), and the frontend has no approve button. |

`IncidentRun`: `{id, project_id, trace_id, uptrace_exception_id, temporal_workflow_id, status, classification, matched_playbook_id, created_playbook_id, playbook_run_id, diagnosis_report, error_message, created_at, updated_at, project_name, playbook, pr_url, playbook_run_status, execution_mode, generate_tests, usage, matched_runbook_id, runbook, telemetry, source, parent_incident_run_id, root_cause, scan_run_id, scan_kind}`. It backs the frontend `/dashboard` table.
- `playbook`: `{id, title, status, source}` or `null` — the matched playbook (`source: "matched"`), else the one written from this incident (`"created"`).
- `runbook`: `{id, title, status, source}` or `null` — the matched runbook (`"matched"`), else the one saved from this incident's successful fix (`"created"`).
- `telemetry`: what was fetched from Uptrace for the alert, `{exception_type, message, stacktrace, service_name, span_name, trace_id, group_id, attrs}` (truncated; `group_id` is Uptrace's error group), or `{}` when nothing was fetched.
- `source`: `alert` · `linked` · `scan`. `parent_incident_run_id`: the incident this one was delegated from (`linked` only), else `null`. `scan_run_id` / `scan_kind`: the remediation agent's scan run and kind (`scan` only), else `null` / `""`.
- `root_cause`: the service mesh's trace walk, `{service_name, span_name, span_id, exception_type, message, path: string[], system, project_id, mapped_by, linked_incident_run_id}` (`path` runs from the trace's root service to the culprit; `project_id` is the culprit's mapped project or `null`; `linked_incident_run_id` is the child incident, if one was started), or `{}` when the mesh is off or the incident has no trace.
- `pr_url` is `""` when no PR was opened; `playbook_run_status` / `execution_mode` / `generate_tests` (the run's frozen setting) are `null` without a playbook run.
- `usage`: `{calls, input_tokens, cached_input_tokens, output_tokens, total_tokens, platform_tokens, models: string[], by_step: [{step, provider, model, billed_to, calls, input_tokens, cached_input_tokens, output_tokens}]}` — one entry per LLM call the incident made (a retried activity counts again; those tokens were spent). `step` is the pipeline step, or `diagnosis_report`. Jev calls report the tokens Cloudflare returns. `cached_input_tokens` is the part of `input_tokens` the provider served from its prompt cache (billed at a discount; 0 where the provider doesn't report it). The Jev calls that help the agent loop (`playbook_execution` with provider `jev_cloudflare`) show as their own `by_step` entry.

`PlaybookRun`: `{id, incident_run_id, playbook_id, runbook_id, execution_mode, generate_tests, status, approved_by_id, approved_at, pr_url, branch_name, attempts: Attempt[]}` where `Attempt` is `{attempt_number, outcome, summary, error_output, generated_steps, branch_name, langfuse_trace_id, created_at}` (up to 3; each re-plans from the previous attempt's error). `approved_by_id` / `approved_at` record whoever decided, for approvals and rejections alike; `approved_by_id` is `null` when the decision was made on GitHub.

### GitHub webhook (called by the GitHub App)

`POST /sre/github/webhook` — no bearer token; GitHub signs the body with the App's webhook secret (`X-Hub-Signature-256`, `GITHUB_APP_WEBHOOK_SECRET`). This is how a **draft-only** run is approved: the agent opens a draft PR, and for `pull_request` events on that PR's branch:
- `closed`, **merged** → approves the fix: the run and incident become `succeeded` and the playbook's outcome is recorded. Also accepted from `rejected`, in case the reopen delivery was lost.
- `closed`, **not merged** → rejects it: the run and the incident become `rejected`. This is distinct from `failed` (the agent couldn't produce a fix, or the pipeline errored): the fix exists, a person declined it.
- `reopened`, or `opened` (a new PR from the same branch), on a **`rejected`** run → back up for review: `pending_approval` / `awaiting_approval`, `approved_at` cleared. The workflow listens for this for **30 days** after a rejection, then ends for good.

The PR must be in the project's repo and on the run's `branch_name`.

With `SRE_REMEDIATION_AGENTS_ENABLED=true` the same endpoint also starts remediation agents (subscribe the App to the **Pull request** and **Push** events): a `pull_request` `closed` + **merged** into a project's default branch starts every enabled `on_merge` agent covering that project, and a `push` event on a branch matching an enabled `branch_watch` agent's `branch_pattern` starts that agent, each for that one repo and diff. This applies to any merge or push, not just the agent's own branches, and returns `200 {detail}` naming the scan runs started. A merge is scanned as the merge commit against its first parent (`<sha>^1`); a push as `before..after`, or against the default branch for a new branch. Branch deletions and pushes with no commits are ignored (`202`). A redelivery starts nothing new. `503 {detail}` if Temporal is unreachable: redeliver the webhook.

With `SRE_GITHUB_AUTO_PROJECTS=true` it also handles `installation` `created` and `installation_repositories` `added` (GitHub always sends these to an App; there is nothing to subscribe to): see "Projects on install" above. Returns `200 {detail}` naming the projects created, else `202`.

Returns `200 {detail}` when it changed a run; `202 {detail}` for anything ignored (other events or actions, PRs the agent isn't waiting on, autonomous runs, an event that doesn't fit the run's state — so redeliveries are harmless — or a workflow that has already finished); `401 {detail}` for a bad signature; `503 {detail}` if `GITHUB_APP_WEBHOOK_SECRET` isn't set, or if the workflow can't be reached (the change is released; redeliver the webhook from the App's settings).

## Errors

All error responses: `{detail: string}` with the appropriate HTTP status. 401 for missing/invalid/expired token, 403 for wrong owner or insufficient project role, 404 for missing resource (or a project you're not a member of), 409 for state conflicts, 422 for validation errors (django-ninja default).
