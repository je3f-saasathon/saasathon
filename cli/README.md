# buggly CLI

Run your app with its errors sent to buggly. Each new error opens an incident, and the agent
diagnoses it and opens a fix PR on your repo.

```bash
# once: install the buggly GitHub App on your repo, then
uvx --from "git+https://github.com/je3f-saasathon/saasathon#subdirectory=cli" buggly login
# every run, from the repo's checkout:
uvx --from "git+https://github.com/je3f-saasathon/saasathon#subdirectory=cli" buggly run python app.py
```

With a local checkout: `uv tool install ./cli`, then `buggly login` and `buggly run …`.

Not on PyPI yet, and this repo is private: the `git+https` form only works for people with access
to it. Once `buggly` is published, it's `uvx buggly run python app.py`.

## Commands

| Command | What it does |
|---|---|
| `buggly login [--api-url URL] [--no-browser]` | Shows a code and opens the browser; approve it there and the CLI saves a 30-day token in `~/.config/buggly/config.json` (0600). |
| `buggly run [--repo owner/name] [--project ID] <command…>` | Finds the repo's project from `git remote`, then runs the command with its telemetry going to that project. The exit code, signals and output are the command's own. |
| `buggly status` | Who you are, and which project and service this repo uses. |
| `buggly logout` | Revokes and forgets the token. |

`BUGGLY_API_URL` points the CLI at another server (e.g. `http://localhost:8300` for `make dev-docker`),
and `BUGGLY_TOKEN` supplies a token without `login` (CI).

## How `run` works

`GET /api/sre/cli/project?repo=owner/name` returns the project's DSN and service name. The CLI
then `exec`s the command with:

- `PYTHONPATH` starting with `src/buggly/bootstrap/`, whose `sitecustomize.py` loads in every
  Python process the command starts (including `uv run …`, Django's reloader and subprocesses);
- the standard `OTEL_*` variables: OTLP/HTTP to the DSN's host with the `uptrace-dsn` header, the
  project's service name, and `vcs.repository.url.full` / `service.version` (the commit) as
  resource attributes. Metrics are off.

`sitecustomize.py` puts the CLI's own OpenTelemetry packages at the *end* of `sys.path` (so an app
with its own OpenTelemetry keeps it), runs OpenTelemetry auto-instrumentation (Django, Flask,
FastAPI, requests, httpx, urllib3, logging, sqlite3, psycopg) and hooks `sys.excepthook` and
`threading.excepthook`: an uncaught exception is recorded on a span and flushed before the process
exits. That exception event is what the platform's Uptrace alert fires on. Any failure there is one
`buggly:` line on stderr; the app runs anyway. It also runs the `sitecustomize` it shadows.

Only Python processes get instrumented. Uptrace's own `uptrace` package isn't used: it only
exports over gRPC, which the platform's Uptrace doesn't accept.

## Development

```bash
cd cli && uv run pytest
```

The tests run crashing scripts, in this interpreter and in a bare venv without OpenTelemetry,
against a fake OTLP collector.
