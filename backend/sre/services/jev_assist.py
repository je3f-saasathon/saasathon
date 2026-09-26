"""Jev helps the agent loop spend fewer chat-model tokens. It can't write code, but it can
answer choice questions cheaply:

- which repo files to read into the first message (fewer exploration turns),
- what a failed command's output shows (a broken environment ends the attempt early),
- which old tool results the agent no longer needs (trimmed from the history),
- whether the agent is stuck (nudged once, then the attempt ends).

It runs when the project's similarity-judge step resolves to Jev (the "OpenAI + Jev" mix,
or a user's own Jev config) and SRE_AGENT_JEV_ASSIST is on. Every question is best effort:
a Jev error is logged and the loop carries on as if Jev weren't there."""

import json
import logging
import re

from django.conf import settings

from ..llm.clients import JevChoice, JevClient, LLMError
from ..llm.resolve import NoLLMConfigError, get_llm_config
from ..models import IncidentRun, LLMProvider, PipelineStep
from .context import incident_context

logger = logging.getLogger(__name__)

MAX_STATE_CHARS = 6000
MAX_FILE_CANDIDATES = 30
MAX_REVIEW_QUESTIONS = 12
PICK_CONFIDENCE = 0.5
DROP_CONFIDENCE = 0.7
STUCK_CONFIDENCE = 0.8
ENV_PROBLEM_CONFIDENCE = 0.9

SOURCE_SUFFIXES = (
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".go", ".rb", ".java", ".kt", ".rs", ".php",
    ".cs", ".html", ".toml", ".cfg", ".ini", ".yaml", ".yml", ".sql",
)

LIST_SOURCE_FILES = (
    "find . -type f -size -100k -not -path './.git/*' -not -path './.venv/*' "
    "-not -path '*/node_modules/*' -not -path '*/__pycache__/*' -not -path './dist/*' "
    "-not -path './build/*' | head -2000"
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

    # ---- which files to preload ------------------------------------------------------

    def rank_candidates(self, listing: str) -> list[str]:
        """Source files from a `find` listing, those named in the incident first."""
        paths = [line.strip().removeprefix("./") for line in listing.splitlines()]
        paths = [p for p in paths if p.endswith(SOURCE_SUFFIXES) and not p.startswith(".")]
        incident = self.state.lower()
        words = set(re.findall(r"[a-z_][a-z0-9_]{3,}", incident))

        def score(path: str) -> tuple:
            lower = path.lower()
            named = lower in incident or lower.rsplit("/", 1)[-1] in incident
            stem_parts = set(re.findall(r"[a-z_][a-z0-9_]{3,}", lower.rsplit(".", 1)[0]))
            is_test = "test" in lower
            return (not named, -len(stem_parts & words), is_test, lower.count("/"), lower)

        return sorted(dict.fromkeys(paths), key=score)[:MAX_FILE_CANDIDATES]

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
