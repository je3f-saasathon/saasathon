"""Jev helps the agent loop spend fewer chat-model tokens. It can't write code, but it can
answer choice questions cheaply:

- where the bug is: twenty questions down the directory tree to the files, then the
  function, plus the matching test file and test command (fewer exploration turns),
- what a failed command's output shows (a broken environment ends the attempt early),
- which old tool results the agent no longer needs (trimmed from the history),
- whether the agent is stuck (nudged once, then the attempt ends).

It runs when the project's similarity-judge step resolves to Jev (the "OpenAI + Jev" mix,
or a user's own Jev config) and SRE_AGENT_JEV_ASSIST is on. Every question is best effort:
a Jev error is logged and the loop carries on as if Jev weren't there."""

import ast
import json
import logging
import re
from dataclasses import dataclass

from django.conf import settings

from ..llm.clients import JevChoice, JevClient, LLMError
from ..llm.resolve import NoLLMConfigError, get_llm_config
from ..models import IncidentRun, LLMProvider, PipelineStep
from .context import incident_context

logger = logging.getLogger(__name__)

MAX_STATE_CHARS = 6000
MAX_FILE_CANDIDATES = 30
MAX_OPTIONS = 30  # per choice question (directories, functions, test files)
MAX_NARROWING_STEPS = 6
MAX_REVIEW_QUESTIONS = 12
PICK_CONFIDENCE = 0.5
DROP_CONFIDENCE = 0.7
STUCK_CONFIDENCE = 0.8
ENV_PROBLEM_CONFIDENCE = 0.9

SOURCE_SUFFIXES = (
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".go", ".rb", ".java", ".kt", ".rs", ".php",
    ".cs", ".html", ".toml", ".cfg", ".ini", ".yaml", ".yml", ".sql",
)

OUTPUT_KINDS = {
    "passed": "The command succeeded; any tests it ran passed",
    "tests_failed": "Tests ran and some of them failed",
    "code_error": "The code could not run: syntax, import, name or type errors in the project",
    "environment_problem": "The environment is broken, not the code: a missing tool or "
                           "dependency, no network, permission denied, a missing interpreter",
    "other": "Something else",
}


class JevAssist:
    def __init__(self, client: JevClient, run: IncidentRun):
        self.client = client
        self.run = run
        self.state = incident_context(run)[:MAX_STATE_CHARS]

    @classmethod
    def for_run(cls, run: IncidentRun) -> "JevAssist | None":
        if not settings.SRE_AGENT_JEV_ASSIST:
            return None
        try:
            config = get_llm_config(run.project, PipelineStep.PLAYBOOK_SIMILARITY_JUDGE)
        except NoLLMConfigError:
            return None
        if config.provider != LLMProvider.JEV_CLOUDFLARE:
            return None
        return cls(JevClient(config), run)

    def _ask(self, state: str, questions: dict, name: str) -> dict[str, JevChoice]:
        try:
            return self.client.choose_many(state, questions, name=name)
        except LLMError as exc:
            logger.warning("Jev assist '%s' failed for incident run %s: %s", name, self.run.id, exc)
            return {}

    # ---- where the bug is: files --------------------------------------------------------

    @staticmethod
    def source_files(paths: list[str]) -> list[str]:
        return list(dict.fromkeys(p for p in paths
                                  if p.endswith(SOURCE_SUFFIXES) and not p.startswith(".")))

    def named_in_incident(self, path: str) -> bool:
        lower, incident = path.lower(), self.state.lower()
        return lower in incident or lower.rsplit("/", 1)[-1] in incident

    def rank(self, paths: list[str]) -> list[str]:
        """Those named in the incident first, then those sharing its words."""
        incident = self.state.lower()
        words = set(re.findall(r"[a-z_][a-z0-9_]{3,}", incident))

        def score(path: str) -> tuple:
            lower = path.lower()
            named = lower in incident or lower.rsplit("/", 1)[-1] in incident
            stem_parts = set(re.findall(r"[a-z_][a-z0-9_]{3,}", lower.rsplit(".", 1)[0]))
            is_test = "test" in lower
            return (not named, -len(stem_parts & words), is_test, lower.count("/"), lower)

        return sorted(dict.fromkeys(paths), key=score)[:MAX_FILE_CANDIDATES]

    def narrow(self, paths: list[str]) -> list[str]:
        """Twenty questions: while there are too many files to ask about one by one, ask
        which directory the bug is in and go down into it. Files the incident names (its
        stack trace) always stay in. Returns the ranked candidates."""
        named = [p for p in paths if self.named_in_incident(p)]
        pool, prefix = [p for p in paths if p not in named], ""
        for _ in range(MAX_NARROWING_STEPS):
            if len(named) + len(pool) <= MAX_FILE_CANDIDATES:
                break
            groups: dict[str, list[str]] = {}
            for path in pool:
                rest = path[len(prefix):]
                groups.setdefault(rest.split("/", 1)[0] if "/" in rest else HERE, []).append(path)
            if len(groups) == 1:
                (only, files), = groups.items()
                if only == HERE:
                    break
                prefix += only + "/"
                continue
            biggest = sorted(groups, key=lambda g: -len(groups[g]))[:MAX_OPTIONS]
            options = {g: ("files directly here" if g == HERE else f"directory {prefix}{g}/")
                          + ": " + ", ".join(p.rsplit("/", 1)[-1] for p in groups[g][:6])
                       for g in biggest}
            answer = self._ask(self.state, {"where": (
                "Which part of the repository holds the code this bug is in?", options)},
                "agent_locate_directory").get("where")
            if answer is None or answer.choice not in groups:
                break
            pool = groups[answer.choice]
            if answer.choice == HERE:
                break
            prefix += answer.choice + "/"
        return self.rank(named + pool)

    def pick_files(self, candidates: list[str], limit: int) -> list[str]:
        if not candidates or limit <= 0:
            return []
        questions = {
            f"f{i}": (f"Will fixing this bug need reading or changing the file `{path}`?",
                      {"yes": "Needed to understand or fix the bug", "no": "Not needed"})
            for i, path in enumerate(candidates)
        }
        answers = self._ask(self.state, questions, "agent_preload_files")
        picked = [(answers[f"f{i}"].confidence, i, path) for i, path in enumerate(candidates)
                  if f"f{i}" in answers and answers[f"f{i}"].choice == "yes"
                  and answers[f"f{i}"].confidence >= PICK_CONFIDENCE]
        return [path for _, _, path in sorted(picked, key=lambda t: (-t[0], t[1]))[:limit]]

    # ---- where the bug is: function, tests, test command -------------------------------

    def pick_function(self, path: str, source: str) -> "Location | None":
        """Python only: the function (or method) in `path` the fix should change."""
        functions = python_functions(source)
        if not functions:
            return None
        functions.sort(key=lambda f: f.name.split(".")[-1].lower() not in self.state.lower())
        functions = functions[:MAX_OPTIONS - 1]
        options = {f.name: f"lines {f.start}-{f.end}: {f.signature}" for f in functions}
        options["none"] = "None of these functions"
        answer = self._ask(self.state, {"function": (
            f"Which function in `{path}` must change to fix this bug?", options)},
            "agent_locate_function").get("function")
        if answer is None or answer.confidence < PICK_CONFIDENCE:
            return None
        return next((f for f in functions if f.name == answer.choice), None)

    def pick_test_file(self, target: str, test_files: list[str],
                       read=lambda path: "") -> str | None:
        """The test file next to the code (tests.py, test_<module>.py) without asking;
        otherwise Jev chooses, seeing each file's test names."""
        if not test_files:
            return None
        directory, _, filename = target.rpartition("/")
        module = filename.rsplit(".", 1)[0]
        prefix = f"{directory}/" if directory else ""
        obvious = [t for t in test_files if t in (
            f"{prefix}tests.py", f"{prefix}test_{module}.py", f"{prefix}{module}_test.py",
            f"{prefix}tests/test_{module}.py", f"tests/test_{module}.py")]
        if obvious:
            return obvious[0]
        ranked = sorted(test_files, key=lambda t: (not t.startswith(prefix), t))[:MAX_OPTIONS - 1]
        options = {t: "tests: " + (", ".join(f.name for f in python_functions(read(t))[:8])
                                   or "unknown") for t in ranked}
        options["none"] = "None of these tests cover it"
        answer = self._ask(self.state, {"tests": (
            f"Which test file tests the code in `{target}`?", options)},
            "agent_locate_tests").get("tests")
        if answer is None or answer.choice == "none" or answer.confidence < PICK_CONFIDENCE:
            return None
        return answer.choice if answer.choice in ranked else None

    def pick_test_command(self, candidates: list[str], evidence: str) -> str | None:
        if len(candidates) <= 1:
            return candidates[0] if candidates else None
        options = {c: "command" for c in candidates}
        answer = self._ask(evidence[:MAX_STATE_CHARS], {"command": (
            "Which command runs this project's tests?", options)},
            "agent_test_command").get("command")
        return answer.choice if answer is not None and answer.choice in options else candidates[0]

    # ---- what a failed command shows -------------------------------------------------

    def read_output(self, command: str, output: str) -> JevChoice | None:
        state = json.dumps({"command": command, "output": output[-3000:]})
        answers = self._ask(state, {"kind": ("What does this command's output show?",
                                             OUTPUT_KINDS)}, "agent_read_output")
        return answers.get("kind")

    # ---- mid-loop review: what to trim, and is the agent stuck -----------------------

    def review(self, history, candidates: list[int]) -> tuple[set[int], JevChoice | None]:
        """Returns (turn indexes whose results can go, the progress verdict)."""
        candidates = candidates[:MAX_REVIEW_QUESTIONS]
        questions = {
            f"t{i}": (
                f"Earlier the agent ran {history.turns[i].describe()}, which returned "
                f"(first lines): {history.turns[i].observation[:300]!r}. Does the agent still "
                "need this result to finish fixing the bug?",
                {"keep": "Still needed", "drop": "No longer needed"},
            )
            for i in candidates
        }
        questions["progress"] = (
            "The coding agent's actions so far, oldest first:\n" + history.action_log()
            + "\nIs the agent making progress towards a working fix?",
            {"progressing": "Making progress", "stuck": "Repeating itself or going in circles"},
        )
        answers = self._ask(self.state, questions, "agent_review")
        drop = {i for i in candidates if f"t{i}" in answers and answers[f"t{i}"].choice == "drop"
                and answers[f"t{i}"].confidence >= DROP_CONFIDENCE}
        return drop, answers.get("progress")


HERE = "(this directory)"


@dataclass
class Location:
    name: str  # "create_order" or "OrderService.create"
    start: int
    end: int
    signature: str


def python_functions(source: str) -> list[Location]:
    """Top-level functions and methods, parsed (never run) from the repo's source."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    found = []

    def add(node, prefix=""):
        args = ", ".join(a.arg for a in node.args.args)
        found.append(Location(prefix + node.name, node.lineno, node.end_lineno or node.lineno,
                              f"def {node.name}({args})"))

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            add(node)
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    add(item, node.name + ".")
    return found


def is_test_file(path: str) -> bool:
    name = path.rsplit("/", 1)[-1].lower()
    return (name.startswith("test") or name.endswith(("_test.py", ".test.js", ".test.ts",
                                                       ".spec.js", ".spec.ts", ".test.tsx"))
            or "/tests/" in f"/{path.lower()}")


def test_command_candidates(files: set[str], read) -> list[str]:
    """The ways this repo's tests could run, from its files; `read(path)` gives contents."""
    candidates = []
    python = {"pyproject.toml", "uv.lock", "requirements.txt"} & files
    if python:
        # Matches install_commands: uv for pyproject repos, else a .venv from requirements.
        python_cmd = "uv run python" if {"pyproject.toml", "uv.lock"} & files else ".venv/bin/python"
        config = " ".join(read(f) for f in ("pyproject.toml", "requirements.txt") if f in files)
        if "pytest" in config:
            candidates.append(f"{python_cmd} -m pytest -q")
        if "manage.py" in files:
            candidates.append(f"{python_cmd} manage.py test")
    if "package.json" in files and '"test"' in read("package.json"):
        candidates.append("npm test")
    return candidates
