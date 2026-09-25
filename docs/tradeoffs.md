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

## 3. One stateless workflow per trace id, no incident grouping

Each Uptrace callback starts one workflow, keyed by `(project, trace_id)`. Repeats of the *same* trace are deduplicated; different traces of the *same bug* are not.

**What's given up**
- A hot crash loop (the same bug throwing 50 times an hour) starts 50 independent workflows. Each runs the full pipeline and may open its own PR. Nothing guards against this beyond whatever throttling Uptrace does before calling us.

**Why not build grouping now**
- Fingerprinting is hard to get right. Is "the same incident" the same trace template, the same top stack frame, or the same file the playbook touches? A wrong heuristic (merging distinct bugs, or grouping nothing useful) is worse than none. v1 ships something real and simple first.

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

## Future improvements the schema doesn't block

- **Incident grouping**: add an `IncidentFingerprint` model (a hash of the normalized stack trace, or Uptrace's own group id) with a foreign key from `IncidentRun`, and check for a recent open run before starting a workflow. The workflow id can switch from the raw trace id to the fingerprint without touching other models.
- **Semantic playbook search**: `PlaybookSearch` ranks by keyword overlap in Python (so it works on SQLite). A `Playbook.embedding` column (pgvector on Postgres) can sit next to `keywords` without a breaking migration; only `PlaybookSearch.top_k` changes.
