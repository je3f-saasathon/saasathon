"""The built-in generic playbooks we ship: one per bug category (services/triage.py
CATEGORIES). Loaded by a data migration and `manage.py seed_playbooks`, matched by `slug`.
Plain data only: migrations import this module.

Steps are generic (investigate / change / verify) and never name files, since they
apply to every repo; the specific files and commands live in each project's runbooks.
"""

VERSION = 1


def _steps(investigate: list[str], change: list[str], verify: list[str]) -> list[dict]:
    return ([{"type": "investigate", "instructions": i} for i in investigate]
            + [{"type": "change", "instructions": c} for c in change]
            + [{"type": "verify", "instructions": v} for v in verify])


BUILTIN_PLAYBOOKS = [
    {
        "slug": "null-reference",
        "category": "null_reference",
        "title": "Null / None dereference in application code",
        "description": "A value that's sometimes missing (None, null, undefined) is used as if it "
                       "were always present, e.g. an attribute read on an optional relation.",
        "symptoms": "AttributeError: 'NoneType' object has no attribute …; TypeError: Cannot read "
                    "properties of undefined/null; NullPointerException; 500s on one route for "
                    "some records only.",
        "keywords": ["nonetype", "attributeerror", "null", "undefined", "typeerror",
                     "nullpointerexception", "optional"],
        "steps": _steps(
            ["Find the frame in the stack trace where the missing value is used, and the "
             "expression that produced it.",
             "Work out why it can be missing: an optional relation, a lookup that can return "
             "nothing, legacy rows, or an unset field."],
            ["Handle the missing case where the value is produced or first used: a sensible "
             "default, a clear 4xx for bad input, or skipping the optional work. Don't just "
             "swallow the error."],
            ["Add or run a test with the value missing, then run the existing tests for that "
             "area."],
        ),
    },
    {
        "slug": "timeout",
        "category": "timeout",
        "title": "Slow call or timeout",
        "description": "A request, query or outbound call takes longer than its time limit, often "
                       "because of work that grows with the data (N+1 queries, unbounded loops).",
        "symptoms": "TimeoutError, ReadTimeout, 504s, statement timeout, requests that slow down "
                    "as data grows.",
        "keywords": ["timeout", "timeouterror", "readtimeout", "504", "slow", "n+1", "latency"],
        "steps": _steps(
            ["From the trace, find which span takes the time: a query, an external call or "
             "application code.",
             "Check whether the cost grows with input size (a query per item, a missing index, "
             "an unbounded loop or page size)."],
            ["Remove the repeated work (batch or prefetch, add pagination or limits), or add a "
             "timeout with a handled fallback for a slow dependency."],
            ["Add a test that fails if the work is repeated per item (for example a query-count "
             "assertion), then run the existing tests."],
        ),
    },
    {
        "slug": "database",
        "category": "database",
        "title": "Database query, constraint, migration or connection error",
        "description": "A query fails: a constraint is violated, the schema doesn't match the "
                       "code, a lock or connection limit is hit, or a query is malformed.",
        "symptoms": "IntegrityError, OperationalError (no such column / database is locked), "
                    "ProgrammingError, unique constraint failed, too many connections.",
        "keywords": ["integrityerror", "operationalerror", "programmingerror", "constraint",
                     "migration", "locked", "deadlock", "connection"],
        "steps": _steps(
            ["Read the database error and find the query and the model or table involved.",
             "Decide which kind it is: bad data reaching a constraint, code ahead of or behind "
             "the schema, concurrency (locks, races), or connection exhaustion."],
            ["Fix it at the source: validate before writing, handle the expected conflict "
             "(get-or-create, retry on lock), add the missing migration, or release connections. "
             "Never drop the constraint to make the error go away."],
            ["Add a test that reproduces the failing write or query, then run the tests for "
             "that area."],
        ),
    },
    {
        "slug": "dependency-failure",
        "category": "dependency_failure",
        "title": "Downstream service or third-party API failure",
        "description": "A call to another service fails or returns an error, and our code doesn't "
                       "handle it, so our request fails too.",
        "symptoms": "ConnectionError, 502/503 from an upstream, HTTPError, unhandled error "
                    "response bodies, cascading failures under load.",
        "keywords": ["connectionerror", "httperror", "502", "503", "upstream", "retry",
                     "circuit", "api"],
        "steps": _steps(
            ["Find the outbound call in the trace and what it returned (status, error, time).",
             "Check how the code handles failures of that call: retries, timeouts and what the "
             "user gets."],
            ["Handle the failure explicitly: a bounded retry with backoff for transient errors, "
             "a timeout, and a clear error or fallback instead of an unhandled exception."],
            ["Add a test with the dependency mocked to fail, then run the existing tests."],
        ),
    },
    {
        "slug": "configuration",
        "category": "configuration",
        "title": "Missing or wrong configuration",
        "description": "A setting, environment variable or secret is missing, misspelled or has "
                       "the wrong format in one environment.",
        "symptoms": "KeyError on an env var, ImproperlyConfigured, errors only in one "
                    "environment, features silently off.",
        "keywords": ["config", "configuration", "env", "environment", "settings", "keyerror",
                     "improperlyconfigured", "secret"],
        "steps": _steps(
            ["Find which setting is read where it fails, and where its value is supposed to "
             "come from."],
            ["Fail fast with a clear message when a required setting is missing, give optional "
             "settings a safe default, and document the variable in the example env file. "
             "Never commit a real secret."],
            ["Add a test for the missing and the malformed value, then run the existing tests."],
        ),
    },
    {
        "slug": "validation",
        "category": "validation",
        "title": "Bad input not handled",
        "description": "Input from a user or client isn't validated, so a malformed value reaches "
                       "code that assumes it's valid and fails with a 500.",
        "symptoms": "ValueError / KeyError / JSONDecodeError on request data, 500s that should "
                    "be 400s, crashes on empty or oversized input.",
        "keywords": ["validation", "valueerror", "keyerror", "jsondecodeerror", "input", "400",
                     "request"],
        "steps": _steps(
            ["Find where the request data enters and which value or shape breaks the code."],
            ["Validate at the boundary and return a clear 4xx with what was wrong, instead of "
             "letting the error surface as a 500."],
            ["Add tests for the bad inputs seen in the incident and for a valid request, then "
             "run the existing tests."],
        ),
    },
    {
        "slug": "resource-exhaustion",
        "category": "resource_exhaustion",
        "title": "Memory, disk, file handles or rate limits exhausted",
        "description": "Work loads too much at once or holds resources too long, until a limit is "
                       "reached.",
        "symptoms": "MemoryError, OOM kills, too many open files, disk full, 429 rate limit "
                    "responses, workers restarting.",
        "keywords": ["memoryerror", "oom", "memory", "disk", "file", "handles", "429",
                     "ratelimit", "exhausted"],
        "steps": _steps(
            ["Find what grows: whole result sets loaded into memory, files or connections not "
             "closed, or calls made faster than a limit allows."],
            ["Stream or paginate large work, close resources deterministically (context "
             "managers), and throttle or back off against rate limits."],
            ["Add a test that exercises the large case in a bounded way, then run the existing "
             "tests."],
        ),
    },
    {
        "slug": "logic-error",
        "category": "logic_error",
        "title": "Wrong behavior in application code",
        "description": "The code runs but computes the wrong result: a wrong condition, off-by-one, "
                       "double-applied transformation or a race between requests.",
        "symptoms": "Wrong totals, dates or states; assertion errors; inconsistent data; errors "
                    "only under concurrency.",
        "keywords": ["logic", "wrong", "incorrect", "race", "assertionerror", "calculation",
                     "timezone", "offbyone"],
        "steps": _steps(
            ["Reproduce the wrong result from the incident's data and find the first place the "
             "value goes wrong."],
            ["Fix the rule at that place, keeping the change as small as possible. For races, "
             "make the update atomic (a transaction, select-for-update or a conditional update)."],
            ["Add a test with the incident's values that fails before the fix, then run the "
             "existing tests."],
        ),
    },
    {
        "slug": "unhandled-exception",
        "category": "other",
        "title": "Unhandled exception (general)",
        "description": "Any error that doesn't fit a more specific playbook: an exception that "
                       "escapes a request or job.",
        "symptoms": "A 500 or failed job with a stack trace.",
        "keywords": ["exception", "error", "500", "traceback", "unhandled"],
        "steps": _steps(
            ["Read the stack trace from the top frame in the application's own code, and "
             "reproduce the failure."],
            ["Fix the cause at the frame where the assumption breaks. Only catch an exception "
             "where there's a correct way to handle it."],
            ["Add a test that reproduces the incident, then run the existing tests."],
        ),
    },
]
