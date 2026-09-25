# SRE agent (`backend/sre/`) — context for new sessions

Last updated 2026-09-26. Branch `feat/sre-agent` (pushed, no PR yet; it also carries the
`/dashboard` commits from `feat/debug-dashboard`). The repo root `CLAUDE.md` is a copy of `AGENTS.md`
made by `make sync-agents`: edit `AGENTS.md`, not the root `CLAUDE.md`, and ask before touching
`AGENTS.md`.

## What this is

An LLM SRE agent. Uptrace calls a webhook with a trace/exception id; a Temporal workflow triages it and,
depending on the project's execution mode, writes a diagnosis or has an LLM agent fix the bug in a
sandbox and open a PR through a GitHub App.

```
Uptrace ─POST /api/sre/webhooks/uptrace/{project_id}─▶ Django (HMAC or shared-secret header)
  └▶ Temporal IncidentDiagnosisWorkflow (one per (project, trace_id); repeats are no-ops)
       confirm_anomaly ─(noise)─▶ no_anomaly
       classify_bug (emits keywords)
       find_candidate_playbooks (keyword overlap, Python, works on SQLite) ─▶ judge_playbook_match
       ├─ no match  ─▶ create_playbook (UNCONFIRMED) ─▶ new_playbook_created   (never executes first time)
       └─ match     ─▶ create_playbook_run (freezes mode; UNCONFIRMED capped at draft_only)
            advisory_only ─▶ write_diagnosis_report ─▶ advisory_complete
            else up to 3 attempts of run_playbook_attempt (agent loop in sandbox; each re-plans from the last error)
              autonomous ─▶ ready PR opened ─▶ succeeded
              draft_only ─▶ GitHub *draft* PR ─▶ wait for approval signal
                              approve ─▶ open_pull_request (marks ready) ─▶ succeeded
                              reject  ─▶ close_pull_request ─▶ failed
            3 failures ─▶ failed; playbook FAILING after 3 failed runs in a row
```

## Files

- `models.py` — Project, ProjectMembership (owner/admin/viewer), LLMProviderConfig (**owned by a
  user**, API key Fernet-encrypted), LLMStepOverride, Playbook, IncidentRun, PlaybookRun,
  PlaybookExecutionAttempt.
- `api.py` / `schemas.py` — `/api/sre/*`; contract in `docs/CONTRACTS.md` ("SRE agent" section).
- `permissions.py` — non-member → 404, low role → 403.
- `workflows.py` — control flow + per-activity timeouts/retry policies (never Temporal's retry-forever default).
- `activities.py` — thin wrappers; each LLM activity is one Langfuse trace (session = `incident-run-{id}`).
- `services/` — the service classes each activity runs: `triage.py` (AnomalyChecker, BugClassifier),
  `playbooks.py` (PlaybookSearch, PlaybookJudge, PlaybookAuthor, DiagnosisReporter, `clean_steps`),
  `executor.py` (PlaybookExecutor: clone → agent loop → commit/push → PR), `sandbox.py`, `github.py`,
  `context.py` (wraps telemetry in `<untrusted_data>` blocks).
- `llm/clients.py` — `AnthropicClient`, `OpenAICompatibleClient` (OpenAI + self-hosted), `JevClient`;
  `llm/resolve.py` — step override → project default; Jev allowed only on `JEV_STEPS`.
- `temporal_types.py` (no Django imports: the workflow sandbox loads it), `temporal_client.py`,
  `worker.py` (`python -m sre.worker`), `tracing.py` (Langfuse SDK v4, no-op without keys),
  `crypto.py`, `validators.py` (SSRF check on `base_url`), `sandbox/Dockerfile`.
- Design trade-offs: `docs/tradeoffs.md`. Plan history: `~/.claude/plans/i-am-building-an-drifting-pebble.md`.

## Security invariants — keep these

- The agent's tool calls run only inside the sandbox container: no env vars or secrets, network
  `SRE_SANDBOX_NETWORK`, and only the repo's work tree mounted.
- Git metadata lives outside the work tree (`--separate-git-dir`, the `.git` pointer file is deleted),
  and git always runs with explicit `--git-dir`/`--work-tree` and hooks and fsmonitor disabled. The
  worker holds every secret; it must never run code the agent wrote. `tests/test_git_safety.py` guards this.
- The GitHub token never touches disk: it's passed as a one-off http header.
- Playbooks can't contain PR/push steps (`clean_steps`); the execution mode alone decides PRs.
- LLM API keys are encrypted and never returned by the API. `base_url` must be public https unless
  `SRE_ALLOW_PRIVATE_LLM_URLS=true` (local dev only).
- Webhook: same 401 for an unknown project and a bad signature.

## Running it locally

- `make dev` (native, **SQLite**) or `make dev-docker` / `make tunnel` (docker mode, **Postgres**).
  **These are different databases**; data created in one isn't in the other.
- Infra (`docker-compose.infra.yml`, compose project `saasathon-infra`), started by `make dev` / `make infra-up`:
  - Temporal on **7243**, UI on **8243**. Not 7233/8233: the `koredl` project uses those.
  - Langfuse (self-hosted v4) at **3100**, login `admin@localhost.dev` / `localdev-password`, project
    "SRE agent", keys `pk-lf-local-dev` / `sk-lf-local-dev`.
  - Langfuse v4 is "events-only": use the SDK (`lf.api.observations.get_many(...)`), not the old `/traces` endpoint.
- Tests: `cd backend && uv run pytest` (93 pass). Workflow tests use Temporal's time-skipping server;
  `-m sandbox` tests need Docker and the image (`make sandbox-image`).
- `.env` changes only reach a docker container when it's recreated
  (`docker compose -p saasathon up -d --no-deps sre-worker`), and the worker never auto-reloads code
  (`docker restart saasathon-sre-worker-1`). Restarting only the worker doesn't disturb the tunnel.
- The docker dev stack for this machine uses BACKEND_PORT=8300, FRONTEND_PORT=5473, DB_PORT=5532.
- `make contracts` after any API change. Run the tunnel with `make tunnel CMD=tunnel` to keep the
  current mode; plain `make tunnel` restarts in docker mode.

## Current state (docker-mode Postgres, which the tunnel serves)

- Demo user `demo@example.com` owns two LLM configs: `openai-fast` (gpt-5.4-mini: default for triage)
  and `openai-strong` (gpt-5.5: overrides for playbook_creation and playbook_execution).
- Project 1 `saasathon` (advisory_only, fake installation id): the playbooks and incidents from the
  demo alerts.
- Project 2 `django-buggy-app`: GitHub App installation `164850677`, repo
  `je3f-saasathon/django-buggy-app`, branch `bug/unhandled-exception`, draft_only. A real run fixed the
  bug → PR https://github.com/je3f-saasathon/django-buggy-app/pull/1 (approved; playbook now confirmed).
- GitHub App `sre-app-local` (App ID 5074964, owned by the org). `.env` stores its Client ID; the key is
  at `~/.config/saasathon/github-app-dev.pem`, mounted read-only into the docker worker. Installed on
  `saasathon`, `django-buggy-app` and `fake-repos`.
- Webhook secrets: `~/.config/saasathon/uptrace_webhook_secret` (project 1) and `..._buggy` (project 2).
- `django-buggy-app` has one bug per `bug/*` branch; `BUGS.md` holds the grading notes. The agent can
  currently read `BUGS.md` from its checkout, which spoils grading.
- Local `.env` sets `SRE_SANDBOX_NETWORK=bridge` so the agent can `uv sync` in the sandbox; prod must be `none`.

## Lessons learned (don't repeat)

- OpenAI GPT-5/o-series reject `max_tokens` and non-default `temperature` → the client sends
  `max_completion_tokens` and only sends `temperature` if a config sets it.
- Docker mode used to overwrite `backend/.venv` as root → fixed with a `/app/.venv` anonymous volume.
- The `docker:dind` image mounts a tmpfs over `/tmp`: never put the prod `SRE_WORKDIR` under `/tmp`.
- Docker's `put_archive` extracts files as root → the sandbox stamps the worker's uid on each tar entry.
- Verify claims against the running system (Langfuse, Temporal, GitHub) before reporting them.

## NEXT STEP: more model providers (Jev, Anthropic, open weights)

Goal: run every pipeline step on each provider family, compare quality/cost/latency in Langfuse, and
pick per-step defaults. The client code exists for all three, but **only OpenAI has been run for real**.

1. **Jev (Cloudflare Workers AI, `jev_cloudflare`)**
   - `JevClient` asks one `choice` question through `jev.client.run_jev`. Only
     `anomaly_double_check`, `bug_classification` and `playbook_similarity_judge` are allowed
     (`JEV_STEPS`); it can't author or execute playbooks. Classification gets its keywords from
     `heuristic_keywords` because Jev can't emit them.
   - It currently uses the **server's** `CLOUDFLARE_ACCOUNT_ID` / `CLOUDFLARE_API_TOKEN`, not the user's
     config. Make it per-user: token in `api_key_encrypted`, account id in `extra_config`, which means
     extending `run_jev` or adding a variant that takes credentials.
   - Never live-tested: needs Cloudflare credentials and gateway balance/BYOK (otherwise it returns 402).
     Also check the question types (`noul`/`score`) — `score` could give the judge a confidence value.
2. **Anthropic (`anthropic`)**
   - `AnthropicClient` exists and is untested live. Use the `claude-api` skill for current model ids and
     parameters; don't guess from memory. Check `max_tokens` limits, whether temperature is supported
     (the client omits it unless configured), and whether structured outputs or tool use should replace
     "reply with JSON only" for the agent loop.
   - Add a live smoke test like the OpenAI one: a signed webhook to a project whose configs are
     Anthropic, and verify the traces through the Langfuse SDK.
3. **Open weights (`self_hosted`, any OpenAI-compatible server: Ollama, vLLM, LM Studio)**
   - `OpenAICompatibleClient` sends `max_tokens` to self-hosted servers. Only tested against a fake
     server so far.
   - Locally: e.g. Ollama at `http://localhost:11434/v1` needs `SRE_ALLOW_PRIVATE_LLM_URLS=true`; from
     the docker worker, use `http://host.docker.internal:11434/v1`.
   - Expect weaker JSON discipline: consider `response_format={"type":"json_object"}` where supported
     and a retry on a `parse_json` failure (currently an `LLMError`, retried by the activity policy).
     The agent loop already tells the model when a reply was invalid.
   - Decide whether open weights are for triage only or also for the agent loop (needs a
     long-context, tool-competent model).
4. **Evaluation harness.** Replay the `django-buggy-app` `bug/*` branches (5 bugs) across provider
   and step combinations, grade against `BUGS.md` (kept away from the agent — see above), and
   compare in Langfuse (group by tag `project:{id}`, model, session).
5. **Model config management.** There's no UI yet; configs are created via `/api/sre/llm-configs` or
   the ORM. `extra_config` supports `max_tokens` and `temperature`.

## Other open items (not started)

- **Security: GitHub connect flow.** Projects take a raw `github_installation_id`, and nothing checks
  that it belongs to the user, so a user could point a project at another customer's installation.
  Build: install link with a signed `state` → Setup URL endpoint → exchange the OAuth `code` → confirm
  via `GET /user/installations` → store it, and remove the manual field. The prod app needs "Any
  account", a Setup/Callback URL, and "Request user authorization during installation". **Do this
  before real users.**
- An approve/reject UI in the frontend (approval is API-only today), and wiring the `/dashboard` mock
  table to `/api/sre/incident-runs`.
- Prod: provision Temporal and Langfuse (not in `docker-compose.prod.yml`), create the prod GitHub App
  and `.env.prod` values, and run `make sandbox-image-prod` (see `docs/DEPLOYMENT.md`).
- Incident grouping (a crash loop means N workflows and N PRs) and semantic playbook search
  (`docs/tradeoffs.md`).
- Already broken before this branch: `scripts/test-scripts.sh` test 1 fails, and Node 26 vs `.nvmrc` 20.17.0.
