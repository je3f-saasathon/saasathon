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

## Future improvements the schema doesn't block

- **Own incident grouping** (if Uptrace's proves wrong): add an `IncidentFingerprint` model (a hash of the normalized stack trace) with a foreign key from `IncidentRun`, and check for a recent open run before starting a workflow. The workflow id can switch from the raw trace id to the fingerprint without touching other models.
- **Semantic playbook search**: `PlaybookSearch` ranks by keyword overlap in Python (so it works on SQLite). A `Playbook.embedding` column (pgvector on Postgres) can sit next to `keywords` without a breaking migration; only `PlaybookSearch.top_k` changes.
