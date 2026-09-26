# Service mesh & active remediation

Two features for the SRE agent (`backend/sre/`), built on branch `feat/mesh-active-remediation`:

1. **Service mesh**: the agent learns how your services call each other from Uptrace, follows an
   error across repositories to the service that actually caused it, and sends the fix to that
   service's repo.
2. **Active remediation**: agents you start yourself, which look for bugs across your repos
   *before* they alert. They run playbooks and runbooks against each repo and feed what they
   find into the same pipeline an Uptrace alert goes through.

The endpoints are in `docs/CONTRACTS.md` ("Service mesh" and "Remediation agents"). The
trade-offs are in `docs/tradeoffs.md` §7–§9. This file explains what the features do, why they
were designed this way, and how to use them.

**Status:** both features are built and tested on this branch, behind
`SRE_SERVICE_MESH_ENABLED` and `SRE_REMEDIATION_AGENTS_ENABLED`. They haven't yet been run
end to end against real multi-service traffic, and there's no frontend yet. See the checklist at
the end.

---

## Background: how Uptrace sees your services

Uptrace doesn't know about your repositories or your docker compose file. It builds its map
from the trace data your services send:

- **Nodes** are `service.name` values (the OTel resource attribute). Each process that sends
  spans is one node. If you leave it unset, SDKs send `unknown_service:<lang>`, and different
  services merge into one node.
- **Edges** come from spans whose parent is in a different service in the same trace. A `client`
  span in service A with a `server` child span in service B gives the edge A → B, with call
  count, error rate and latency. Message queues work the same way with `producer`/`consumer`
  spans.
- **Uninstrumented dependencies** (Postgres, Redis, external APIs) become nodes too, from client
  span attributes such as `db.system`, `messaging.system` and `server.address`.

Uptrace serves this graph from `GET /internal/v1/service-graph/{project}`. The local Uptrace 2.1
in `infra/uptrace/` returns
`{edges: [{type, clientAttr, clientName, serverAttr, serverName, count, errorCount, errorRate, durationAvg, durationMax, rate}], totalEdges}`.
`serverAttr` is `service_name` for a service and `_system` for a dependency such as
`db:sqlite`. A whole trace comes from `GET /internal/v1/traces/{project}/{trace_id}`: a flat
list of `spans`, each with `id`, `parentId`, `kind`, `statusCode`, `attrs.service_name` and its
`logs` (exceptions included).

### Instrumenting several repos under one compose file

For the map to connect your services, every service must:

1. Send to the **same Uptrace project** (same DSN). Edges only form within one project.
2. Use a **distinct `service.name`**.
3. **Pass on trace context** on every call between services: the W3C `traceparent` header,
   which standard HTTP client/server instrumentation handles for you. A hop that drops the
   header (a hand-rolled HTTP call, a queue without context injection, a proxy that strips
   headers) splits the trace, and that edge disappears. A browser frontend only links if the
   backend's CORS config allows `traceparent`.
4. Use the right **span kinds** (`client`/`server`, `producer`/`consumer`). Auto-instrumentation
   sets them, but a manual span defaults to `internal` and produces no edge.
5. Set **`vcs.repository.url.full`** (recommended) so each span says which repo it came from.
   This is how the agent maps a service to a project (see below).

For example, in the meta compose file:

```yaml
x-otel: &otel
  OTEL_EXPORTER_OTLP_ENDPOINT: http://uptrace:14418
  OTEL_EXPORTER_OTLP_HEADERS: uptrace-dsn=${UPTRACE_DSN}
  OTEL_PROPAGATORS: tracecontext,baggage

services:
  api:
    environment:
      <<: *otel
      OTEL_SERVICE_NAME: api
      OTEL_RESOURCE_ATTRIBUTES: vcs.repository.url.full=https://github.com/acme/api
  worker:
    environment:
      <<: *otel
      OTEL_SERVICE_NAME: worker
      OTEL_RESOURCE_ATTRIBUTES: vcs.repository.url.full=https://github.com/acme/worker
```

---

## Feature 1: service mesh

### What it does

- **Keeps a service graph for each organization and Uptrace project.** Nodes are services and
  dependencies. Edges carry call count, errors and latency. It's refreshed from Uptrace's
  service graph every 15 minutes, and each alert's trace adds its edges as it arrives. An edge
  not seen for 7 days (`SRE_SERVICE_GRAPH_TTL_DAYS`) is dropped. It isn't dropped just because
  one trace didn't take that path.
- **Maps each service to a project (repo).** It prefers the `vcs.repository.url.full` attribute
  on the service's spans. Otherwise it uses the project's `service_names` list. A service matching
  neither is shown as unmapped.
- **Finds where an error started.** A new pipeline step, `localize_root_cause`, runs right after
  the telemetry fetch. It walks the alert's whole trace and finds the deepest error span that
  has no failing children. That span's service is the **culprit**, and the services from the
  trace's root down to it are the **propagation path**. Both are stored on the incident as
  `root_cause`. The step is plain code, not an LLM call.
- **Hands the incident to the right repo.** If the culprit maps to a *different* project in the
  same org with the same pinned Uptrace project, the incident gets a **linked child incident**
  in that project. The child uses that project's execution mode, playbooks, runbooks and
  models. The original incident ends as `delegated` and points at the child. Anywhere else (an
  unmapped culprit, another org, another Uptrace project), the incident stays where it is and
  its diagnosis names the culprit.
- **Lets the fix agent read the neighbours.** When the agent fixes a service, the repos of the
  services that call it or that it calls are cloned into the sandbox **read-only** under
  `/neighbours/<owner>-<repo>`, so it can see how the other side uses the code. Only the repo
  being fixed is writable. Only repos on the same GitHub App installation (the same GitHub
  account) are shared. Otherwise the agent could copy code from a repo some reviewers can't
  see into this repo's PR.

### How to use it

1. Set `SRE_SERVICE_MESH_ENABLED=true` (and `SRE_UPTRACE_FETCH_ENABLED=true`, which the
   mesh builds on) in `backend/.env`, then recreate the backend and worker containers.
2. Instrument your services as described above, all sending to one Uptrace project.
3. Create one project per repo in the **same organization**, and give each the same
   `uptrace_source_id` (e.g. `uptrace.buggly.dev/1`) and an Uptrace credential
   (Settings → Organization → Uptrace).
4. If your services don't send `vcs.repository.url.full`, list each project's service names:
   `PATCH /api/sre/projects/{id}` with `{"service_names": ["worker", "worker-cron"]}`.
5. Point the Uptrace alert channel at **any one** of these projects' webhook URLs. That project
   works as the inbox, and the mesh routes each incident to the repo where it started.
6. See the map with `GET /api/sre/organizations/{id}/service-graph`. Refresh it now with
   `POST /api/sre/organizations/{id}/service-graph/refresh` (org admin).
7. On the dashboard, an incident shows its `root_cause` (culprit, path and repo), and a delegated
   incident links to its child.

---

## Feature 2: active remediation agents

### What it does

An org admin starts **remediation agents**. Each agent has a **kind** (what it looks for), a
**trigger** (when it runs) and a set of the org's projects (repos). A run of an agent is a
**scan run**. It scans each repo with an LLM agent in a read-only sandbox, **at most 2 repos at
a time**. Each bug it finds becomes an incident with `source: "scan"`, and that incident goes
through **the same pipeline as an Uptrace alert**.

| Kind | Looks for | Built on |
|---|---|---|
| `playbook_sweep` (default) | Bugs matching any of the chosen playbooks (default: every non-failing playbook the project can see) | Playbooks |
| `runbook_variant` | New occurrences of bugs a runbook already fixed, in the same repo or others. Mesh neighbours of the fixed service are checked first | Confirmed runbooks + the mesh |
| `find_quiet` | Uptrace error groups and slow or erroring mesh edges that never triggered an alert, for the services mapped to the repo | Uptrace + the mesh |

| Trigger | Runs when | Scans |
|---|---|---|
| `on_merge` (default) | A PR is merged into a project's default branch (the GitHub App webhook) | Only the merged diff |
| `branch_watch` | A push lands on a branch matching the agent's `branch_pattern` (e.g. `release/*`) | Only the pushed diff |
| `schedule` | The agent's `schedule_cron` fires (a Temporal Schedule) | The whole repo |

Any agent can also be run by hand (`POST /api/sre/agents/{id}/run`).

**What happens to a finding.** The scanner reports each finding in the same shape as Uptrace
telemetry: category, message, location, evidence, service, and a suggested playbook or runbook.
Each finding starts an `IncidentDiagnosisWorkflow`:

- `confirm_anomaly` checks the finding: a second model reviews the scanner's claim, and one that
  doesn't hold up ends as `no_anomaly`. This is the false-positive filter.
- `classify_bug` and matching use the scanner's suggested category, playbook or runbook as hints.
  The judge still has to agree.
- The execution mode is capped by the **evidence** (below). The rest of the pipeline (draft PRs,
  approvals, runbook saving, usage and billing) is unchanged.
- Each finding's incident key is `scan-{kind}-{repo}-{hash(category + location)}`, so scanning
  again never raises the same bug twice. This reuses the pipeline's existing "one workflow per
  incident key" rule.

**How far a scan-found fix can go.** A scan incident's execution mode is the lower of the agent's
`execution_mode` and a cap set by its evidence:

| Finding | Default |
|---|---|
| `runbook_variant` match | `draft_only` (a draft PR) |
| `find_quiet` finding backed by a real trace | `draft_only` |
| `playbook_sweep` finding, or anything without concrete evidence | `advisory_only` (a diagnosis only) |

A scan incident **never** runs `autonomous`, even with a confirmed runbook, because no production
alert confirmed the bug.

**Limits.** Each agent has `max_findings_per_repo` (default 3) and an optional
`monthly_token_budget`. Scans use a new pipeline step, `repository_scan`, which can have its own
model through the project's step overrides like any other step. Tokens on the company default
model count toward the project's monthly cap as usual.

### How to use it

1. Build and enable the mesh first if you want `runbook_variant` ordering or `find_quiet`.
   `playbook_sweep` works without it.
2. Set `SRE_REMEDIATION_AGENTS_ENABLED=true` in `backend/.env`. For `on_merge` and
   `branch_watch`, subscribe the GitHub App to the **Pull request** and **Push** events.
3. Create an agent (org admin):
   `POST /api/sre/organizations/{id}/agents` with
   `{"name": "Nightly sweep", "kind": "playbook_sweep", "trigger": "schedule", "schedule_cron": "0 3 * * *", "project_ids": [1, 2, 3]}`.
   Leave `project_ids` out to cover all of the org's projects.
4. Run it now with `POST /api/sre/agents/{id}/run`, or wait for its trigger.
5. Follow it with `GET /api/sre/agents/{id}/scan-runs` (per-repo progress and findings). Each
   finding shows on the dashboard as an incident with `source: "scan"`.
6. Pause an agent with `PATCH /api/sre/agents/{id}` `{"enabled": false}`. Raise or lower how far
   its fixes can go with `execution_mode` (`advisory_only` or `draft_only`).

---

## How we got here: design decisions

These came out of a design discussion on 2026-09-26. Each row gives what we picked, what we
didn't, and why.

| Decision | Chosen | Alternatives considered | Why |
|---|---|---|---|
| Where the graph comes from | Uptrace's own service graph endpoint, refreshed every 15 min, plus each alert's trace | Computing edges ourselves from sampled traces | Uptrace already aggregates every span. Sampling ourselves costs API calls and is less complete. Alert traces add edges between refreshes. |
| Removing edges | TTL on `last_seen` (7 days) | Delete when a refresh or trace doesn't show it | A trace that didn't take a path says nothing about whether the path still exists. |
| Service → repo | `vcs.repository.url.full` attribute, then `Project.service_names` | An explicit list only; attributes only; an LLM guess | The attribute is exact and needs no upkeep, but not every service sends it. The list covers the rest. |
| Finding the culprit | Deterministic trace walk (deepest error span with no failing children) | Asking the LLM | Cheap, testable and repeatable. The LLM still reads the path afterwards. |
| Culprit in another repo | Linked child incident in the culprit's project; parent ends `delegated` | Moving the incident; advisory only | Each repo's owners keep control of their repo's execution mode and approvals, and the original alert keeps its history. |
| Neighbour repos in the sandbox | Cloned read-only | Fetched on demand through a tool; single-repo only | The agent often needs the other side of a call. Read-only keeps the rule that only one repo is ever writable. |
| Where the graph lives | Per organization and Uptrace project | Per project | A project is one repo, and the graph spans repos. |
| Proactive discovery | Three agent **kinds** the user starts: `playbook_sweep` (default), `runbook_variant`, `find_quiet` | One fixed discovery strategy | Kinds trade coverage against precision differently. Users pick per agent. |
| Parallelism | One scan activity per repo, at most 2 at once per scan run, limited inside the workflow | A worker-wide concurrency limit | A worker-wide limit would also slow down alert handling. |
| What a finding becomes | An incident, `source: "scan"`, through the existing incident workflow | A separate findings pipeline and a `Finding` table | Reuses verification (`confirm_anomaly`), matching, approvals, runbooks, dedup, usage and the dashboard. The scanner only has to produce the context. |
| Output | Execution mode capped by evidence; never `autonomous` | Everything advisory; everything a draft PR; promote from the dashboard | Strong evidence earns a draft PR. A weak sweep finding shouldn't open PRs across ten repos. |
| Triggers | Per agent: `on_merge` (default, diff only), `branch_watch`, `schedule`, plus manual | Schedule only | Merges are when new bugs arrive, and scanning only the diff is cheap. |
| Agent scope | Owned by an org, covering some or all of its projects | One agent per project | Cross-repo kinds need to see several repos at once. |
| Build order | Mesh first, then agents | Agents first; both together | `runbook_variant` ordering and `find_quiet` need the mesh. |

## Build order

1. **Mesh**
   - [x] Contracts and docs
   - [x] `Project.service_names`, `ServiceNode` / `ServiceEdge`, `IncidentRun.source` / `root_cause` / `parent_incident_run`
   - [x] Uptrace client: whole trace + service graph
   - [x] Graph refresh (activity, workflow, schedule, manual endpoint) and `GET …/service-graph`
   - [x] `localize_root_cause` + linked child incidents
   - [x] Read-only neighbour repos in the sandbox
   - [ ] Live check against the local Uptrace + Temporal with two instrumented services
2. **Remediation agents**
   - [x] `RemediationAgent`, `ScanRun`, `ScanRepo`, `repository_scan` step, scan usage counted toward caps
   - [x] `ActiveRemediationWorkflow` (per-repo fan-out, max 2) → child incident workflows
   - [x] `playbook_sweep`, `runbook_variant`, `find_quiet`
   - [x] Triggers: manual, `on_merge`, `branch_watch`, `schedule`
   - [ ] Live check: a real scan through Temporal, and a GitHub merge/push delivery
   - [ ] Frontend: org service map, agents page, `source`/`root_cause` on the dashboard

## Implementation map

| Piece | Where |
|---|---|
| Graph, service → project mapping, trace walk, neighbours | `backend/sre/services/mesh.py` |
| Uptrace routes (trace, service graph, repo grouping, error groups) | `backend/sre/services/uptrace.py` |
| `localize_root_cause`, graph refresh, scan activities | `backend/sre/activities.py` |
| `IncidentDiagnosisWorkflow` (delegation), `ServiceGraphRefreshWorkflow`, `ActiveRemediationWorkflow` | `backend/sre/workflows.py` |
| Schedules (graph refresh, per-agent cron), starting scans | `backend/sre/temporal_client.py` (reconciled on worker start) |
| Read-only neighbour repos | `backend/sre/services/executor.py`, `backend/sre/services/sandbox.py` |
| Scanner (per-kind material, agent loop, finding validation, evidence caps) | `backend/sre/services/scanning.py` |
| Manual / merge / push triggers | `backend/sre/services/agents.py`, the GitHub webhook in `backend/sre/api.py` |
| Agent and scan-run API | `backend/sre/agents_api.py` |
| Scan-aware triage and matching | `AnomalyChecker.SCAN_SYSTEM` (`services/triage.py`), `_add_scan_suggestions` and the mode caps in `activities.py` |

Scanner safeguards:
- The repo is mounted read-only with no network.
- A finding must point at a file that exists in the checkout.
- Playbook, runbook and error-group ids must be ones the scanner was shown.
- At most `max_findings_per_repo` findings are kept.
- `runbook_variant` only reads runbooks from repos on the same GitHub App installation.
- `find_quiet` skips error groups some incident already covers.
