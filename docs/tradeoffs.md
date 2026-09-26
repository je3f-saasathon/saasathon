# SRE agent: design trade-offs

Why the LLM SRE agent (`backend/sre/`) is built the way it is, and what each choice gives up. `docs/ARCHITECTURE.md` describes what exists; this file records why.

## 1. The worker uses the Django ORM directly, not an HTTP boundary back to the API

The Temporal worker is a separate process, but it imports the Django app and calls the ORM itself instead of going through `/api/*` the way `backend/services/*` would.

**Pros**
- No serialization or internal-auth plumbing for what is first-party CRUD on our own tables.
- Activities get Django's transactions, `select_for_update`, `select_related` and model validation directly.

**Cons**
- The worker loads the whole Django app registry, so its deploy and restart lifecycle is coupled to Django migrations. A migration mid-incident can race a long-running activity.
- The worker and web process must ship from the same code revision. "The agent" can't be scaled or versioned independently of "the API" the way a `services/*` service could.

## 2. Per-step LLM configs, not one LLM config per project

A user owns `LLMProviderConfig` rows (provider, model, encrypted key). A project points at a default config, and `LLMStepOverride` can swap in a different config for any of the five pipeline steps.

**Pros**
- Matches how the steps differ: a cheap, fast model for anomaly checks and classification, a stronger one for the agentic edit loop.
- A project with no overrides is exactly as simple as a single foreign key; the extra complexity is opt-in.

**Cons**
- Two tables plus fallback logic is more to test and reason about than one foreign key.
- Adding a sixth pipeline step means a migration and an enum change, touching everything that resolves steps.

## 3. One stateless workflow per incident key, grouping delegated to Uptrace

Each callback starts one workflow, keyed by `(project, incident key)`. For real Uptrace alerts the key is the Uptrace **alert id**, and Uptrace already groups repeats of the same error into one alert. So a crash loop (the same bug throwing 50 times an hour) is one incident and at most one PR. Direct calls with a `trace_id` are keyed by that trace, so different traces of the same bug still start separate workflows.

**What's given up**
- The grouping is only as good as Uptrace's. If it splits one bug into two alerts (say, different span names), that's two incidents. If it merges two bugs into one alert, the second never gets its own run.
- Once an alert has had its incident, it never gets another, even if the fix was rejected and the error comes back. Closing and reopening the alert in Uptrace doesn't help, because it keeps the same id.

**Why not build our own grouping now**
- Fingerprinting is hard to get right, and Uptrace already does it with the full span data. We'd only add our own if its grouping proves wrong in practice.

## 4. GitHub connect: the signed `state` isn't bound to the browser

"Connect GitHub" puts the user id in a signed, 10-minute `state` because GitHub's redirect
back to our callback carries no bearer token (the frontend keeps it in localStorage, not
a cookie).

**What's given up**
- Someone who steals a victim's fresh `state` could finish the flow with *their own* GitHub
  account and link *their* installations to the victim's user. That doesn't give anyone
  access to someone else's repos. At worst it adds installations the victim didn't intend to have.

**Why not bind it now**
- Binding needs a cookie set on the backend's domain before the redirect, which is
  a separate origin from the frontend in dev and in the tunnel. The risk is low and the TTL is short.

## 5. Uptrace telemetry comes from its internal (UI) API

An alert notification only carries the alert's name, so before triage the worker fetches the
exception from Uptrace (`services/uptrace.py`, behind `SRE_UPTRACE_FETCH_ENABLED`). It uses
two calls from Uptrace's own UI API, checked against Uptrace 2.1 (`infra/uptrace/`):
`GET /internal/v1/alerts/{project}/{alert}` gives a sample `traceId`/`spanId` for the alert's
error group, and `GET /internal/v1/traces/{project}/{trace}/{span}` gives that span's
`exception_type`, `exception_stacktrace` and `service_name` attributes.

**What's given up**
- These are `/internal/` routes, not a documented public API, so an Uptrace upgrade can change
  them. A changed shape means an empty or partial `telemetry`, never a failed incident.
- The token is user-scoped: it can read every Uptrace project its user can. Stored once per
  org (`UptraceCredential`), and only ever sent for the project's pinned Uptrace project and
  host.

**Why not the MCP endpoint or the Spans API**
- Uptrace Cloud's MCP server (`/mcp/<project>`) has `get_alert`/`list_spans` tools, but it isn't
  part of the self-hosted instance we run, and the internal routes above are what its UI uses.

## 6. Runbooks are saved from successful fixes, not written up front

With `SRE_RUNBOOKS_ENABLED`, playbooks are generic and runbooks are specific to a project's repo.
A runbook is only saved when an agent's fix succeeds (from what the agent proposes in its
`finish` action, or what it actually did), so no extra LLM call is spent on it.

**What's given up**
- `advisory_only` projects never run the agent, so they never build runbooks: every incident
  there is matched against playbooks only.
- A new runbook starts unconfirmed even though a person approved the fix's PR. That review
  covered the code change, not the runbook, so the runbook earns confirmation on its first
  approved reuse. Until then, runs that follow it stay `draft_only`.
- Built-in playbooks are shared by every org, so runs never change their status or failure count.
  A built-in that fits badly is caught by its runbooks going `failing`, not by the built-in itself.

**Why not generate a runbook for every incident**
- That would cost an LLM call per incident, and the agent would still explore the repo from scratch
  every time. Reusing a proven runbook shortens the agent loop, which is where the tokens go.

## 7. The service graph comes from Uptrace's internal service-graph route

With `SRE_SERVICE_MESH_ENABLED`, the worker copies Uptrace's own service graph
(`GET /internal/v1/service-graph/{project}`, the route its UI's service map uses, checked against
Uptrace 2.1) into `ServiceNode` / `ServiceEdge` every 15 minutes. Each alert's whole trace
(`GET /internal/v1/traces/{project}/{trace}`) adds its edges in between. Edges expire after
`SRE_SERVICE_GRAPH_TTL_DAYS` without being seen.

**What's given up**
- Like §5, these are `/internal/` routes, so an Uptrace upgrade can change them. A changed shape
  means an empty or stale graph, never a failed incident.
- The graph is only as good as the instrumentation. A hop that drops `traceparent`, or a manual
  span left as `internal`, has no edge, and we can't tell a missing edge from an absent one.
- Expiry is a guess. A path used once a week can drop out of a 7-day window and come back.

**Why not compute edges ourselves**
- Uptrace already aggregates every span at ingestion. Sampling traces through its API would cost
  far more calls and still see less.

## 8. Root cause by a deterministic trace walk; the fix moves to the culprit's project

`localize_root_cause` picks the deepest error span with no failing children, with no LLM call. If
that service maps to another project in the same org and Uptrace project, a linked child incident
runs there and the original ends `delegated`.

**What's given up**
- A caller that times out *before* its callee fails looks like the culprit. So does an error that
  one service swallows and re-raises as a different one. The LLM still reads the whole path during
  triage, and can say so in the diagnosis, but it doesn't move the incident.
- The child runs under the other project's settings. An alert that arrived at an `autonomous`
  project can end up as an advisory diagnosis in an `advisory_only` one, which is intended.
- Service → repo mapping trusts `vcs.repository.url.full` from telemetry. A service could claim
  someone else's repo. That only matters inside one org and Uptrace project, whose services are
  already trusted to send alerts, and it only routes to projects in the same org.

**Why not ask the LLM, or move the incident**
- The walk is cheap, testable and gives the same answer on replay. Moving the incident would
  lose the alert's history in the project that received it. A linked child keeps both.

## 9. Remediation agents feed the incident pipeline instead of having their own

A scan's findings become incidents (`source: "scan"`) and run through `IncidentDiagnosisWorkflow`,
with `confirm_anomaly` acting as the finding's second opinion. There's no separate findings
table.

**What's given up**
- The dashboard mixes production alerts with scan findings. `source` is there to filter on.
- A finding is keyed by category + location. A real second bug at the same location in the same
  category, found after the first was raised, is never raised again. The same was accepted for
  Uptrace alerts in §3.
- Every finding costs a full pipeline's worth of triage calls, which is why each agent has
  `max_findings_per_repo` and a token budget.

**Why not a separate pipeline**
- Verification, matching, execution modes, draft PRs, approvals, runbook saving, dedup and usage
  tracking all exist already. The scanner only has to produce the context.

## Future improvements the schema doesn't block

- **Own incident grouping** (if Uptrace's proves wrong): add an `IncidentFingerprint` model (a hash of the normalized stack trace) with a foreign key from `IncidentRun`, and check for a recent open run before starting a workflow. The workflow id can switch from the raw trace id to the fingerprint without touching other models.
- **Semantic playbook search**: `PlaybookSearch` ranks by keyword overlap in Python (so it works on SQLite). A `Playbook.embedding` column (pgvector on Postgres) can sit next to `keywords` without a breaking migration; only `PlaybookSearch.top_k` changes.
