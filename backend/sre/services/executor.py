import json
import logging
import shutil
from pathlib import Path
from typing import Callable

from django.conf import settings

from ..llm.clients import LLMError, client_for, parse_json
from ..llm.resolve import get_llm_config
from ..llm.usage import usage_turn
from ..models import ExecutionMode, PipelineStep, PlaybookExecutionAttempt, PlaybookRun
from ..temporal_types import AttemptResult
from .context import UNTRUSTED_NOTICE, incident_context, untrusted
from . import mesh
from .agent_history import AgentHistory
from .github import GitError, GitHubRepo
from .jev_assist import (
    ENV_PROBLEM_CONFIDENCE, STUCK_CONFIDENCE, JevAssist, is_test_file, test_command_candidates,
)
from .playbooks import clean_steps
from .sandbox import NEIGHBOURS, Sandbox, SandboxError

logger = logging.getLogger(__name__)

MAX_TURNS = 40
# History trimming (see agent_history): every this many turns, leaving the latest few alone.
COMPACT_EVERY_TURNS = 6
KEEP_RECENT_TURNS = 4
# Files Jev picks to read into the first message.
PRELOAD_MAX_FILES = 4
PRELOAD_MAX_CHARS = 24000
# A located file longer than this goes in as an outline plus the located function.
WHOLE_FILE_CHARS = 8000
FUNCTION_CONTEXT_LINES = 5
OUTLINE_MAX_ENTRIES = 40
REPO_MAP_MAX_FILES = 200

WRITE_TESTS = (
    "Add or update a test that covers the fix when the repo has tests. "
)
NO_NEW_TESTS = (
    "Do not write new tests or edit test files, and skip any playbook step that does; "
    "only run the repo's existing relevant tests to check the fix. "
)


PROPOSE_RUNBOOK = (
    "When the fix works, include in finish a runbook for this repo, so the next incident like "
    "this can be fixed faster: \"runbook\": {\"title\": \"...\", \"area\": \"<the part of "
    "the app, e.g. orders API>\", \"steps\": [{\"type\": \"edit_file\", \"path\": \"...\", "
    "\"instructions\": \"...\"}, {\"type\": \"run_command\", \"command\": \"<the test "
    "command>\"}]}. "
)


def agent_system(generate_tests: bool, propose_runbook: bool = False) -> str:
    return (
        "You are an SRE agent fixing a production bug in a git repository checked out at "
        "/workspace. Follow the playbook, adapting it to this incident. Work in small steps: "
        "inspect, edit, then run the relevant tests. "
        + (WRITE_TESTS if generate_tests else NO_NEW_TESTS) + UNTRUSTED_NOTICE + "\n\n"
        "Each reply must be exactly one JSON object choosing one action:\n"
        '{"action": "list_files", "path": "."}\n'
        '{"action": "read_file", "path": "src/app.py"}\n'
        '{"action": "read_file", "path": "src/app.py", "start": 40, "end": 90}  (lines 40-90 only)\n'
        '{"action": "edit_file", "path": "src/app.py", "old": "<exact text to replace>", '
        '"new": "<replacement>"}\n'
        '{"action": "write_file", "path": "src/new_file.py", "content": "<full file content>"}\n'
        '{"action": "run_command", "command": "pytest -x tests/test_app.py"}\n'
        '{"action": "finish", "summary": "<what you changed and why>", "tests_passed": true|false}\n'
        "To change an existing file use edit_file: `old` must match the file exactly once "
        "(include a few surrounding lines). Use write_file only for new files. "
        "Paths are relative to /workspace. You cannot commit, push or open pull requests; "
        "that happens after you finish. Call finish with tests_passed=false if you could not "
        "get the tests passing."
        + (" " + PROPOSE_RUNBOOK if propose_runbook else "")
    )


INSTALL_TIMEOUT_SECONDS = 900


def install_commands(work_tree: Path) -> list[str]:
    """The repo's own lockfile install, chosen by the worker (never by the agent)."""
    commands = []
    if (work_tree / "uv.lock").exists():
        commands.append("uv sync --frozen")
    elif (work_tree / "requirements.txt").exists():
        commands.append("python -m venv .venv && .venv/bin/pip install -r requirements.txt")
    elif (work_tree / "pyproject.toml").exists():
        commands.append("uv sync")
    if (work_tree / "package-lock.json").exists():
        commands.append("npm ci")
    elif (work_tree / "package.json").exists():
        commands.append("npm install")
    return commands


def clean_runbook_draft(raw: dict) -> dict:
    """What the agent proposed, trimmed to known fields and safe step types."""
    return {"title": str(raw.get("title") or "")[:255], "area": str(raw.get("area") or "")[:255],
            "steps": clean_steps(raw.get("steps"))}


def branch_name_for(playbook_run: PlaybookRun, attempt_number: int) -> str:
    return f"sre/incident-{playbook_run.incident_run_id}-a{attempt_number}"


class PlaybookExecutor:
    """One re-plan attempt: clone, run the agent loop in the sandbox, commit + push,
    and (AUTONOMOUS only) open the PR. Returns a result instead of raising for
    ordinary failures so the workflow can feed the error into the next attempt."""

    def __init__(self, playbook_run: PlaybookRun, attempt_number: int, previous_feedback: str,
                 heartbeat: Callable[..., None] = lambda *a: None):
        self.playbook_run = playbook_run
        self.run = playbook_run.incident_run
        self.project = self.run.project
        self.playbook = playbook_run.playbook
        self.runbook = playbook_run.runbook
        self.runbook_draft: dict = {}
        self.attempt_number = attempt_number
        self.previous_feedback = previous_feedback
        self.heartbeat = heartbeat
        self.branch = branch_name_for(playbook_run, attempt_number)
        self.steps: list[dict] = []
        # {directory under /neighbours: (service project, host path)} for the agent's kickoff.
        self.neighbours: dict[str, tuple] = {}

    def execute(self) -> AttemptResult:
        attempt, _ = PlaybookExecutionAttempt.objects.get_or_create(
            playbook_run=self.playbook_run,
            attempt_number=self.attempt_number,
            defaults={"previous_attempt_feedback": self.previous_feedback, "branch_name": self.branch},
        )
        workdir = Path(settings.SRE_WORKDIR) / f"run-{self.playbook_run.id}-a{self.attempt_number}"
        try:
            result = self._execute(workdir)
        except (GitError, SandboxError, LLMError) as exc:
            result = AttemptResult("failed", str(exc), self.branch)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

        attempt.generated_steps = self.steps
        attempt.runbook_draft = self.runbook_draft
        attempt.outcome = result.outcome
        attempt.summary = result.summary
        attempt.error_output = result.error_output
        attempt.save()
        self.playbook_run.branch_name = self.branch
        if result.pr_url:
            self.playbook_run.pr_url = result.pr_url
        self.playbook_run.save(update_fields=["branch_name", "pr_url", "updated_at"])
        return result

    def _execute(self, workdir: Path) -> AttemptResult:
        git_dir, work_tree = workdir / "git", workdir / "tree"
        shutil.rmtree(workdir, ignore_errors=True)
        workdir.mkdir(parents=True)
        repo = GitHubRepo(self.project)
        repo.clone(git_dir, work_tree, self.branch)
        self.heartbeat("cloned")
        self._clone_neighbours(workdir / "neighbours")

        # Dependencies are installed with network, then the sandbox is cut off before the
        # agent (which reads attacker-influenced telemetry) gets a single turn.
        commands = install_commands(work_tree)
        agent_offline = settings.SRE_SANDBOX_NETWORK == "none"
        install_network = settings.SRE_SANDBOX_INSTALL_NETWORK
        start_network = (install_network if commands and agent_offline and install_network != "none"
                         else settings.SRE_SANDBOX_NETWORK)
        name = f"sre-{self.playbook_run.id}-a{self.attempt_number}"
        with Sandbox(work_tree, name=name, network=start_network,
                     neighbours={d: path for d, (_, path) in self.neighbours.items()}) as box:
            install_report = self._install_dependencies(box, commands)
            # What the install wrote (a lockfile the repo doesn't track, say) is the
            # worker's doing, not the fix: it stays out of the PR unless the agent edits it.
            installed = repo.changed_files(git_dir, work_tree)
            if agent_offline and start_network != "none":
                box.isolate()
            self.heartbeat("sandbox ready")
            summary, tests_passed = self._agent_loop(box, install_report)

        now = repo.changed_files(git_dir, work_tree)
        leave_out = sorted(path for path, digest in installed.items() if now.get(path) == digest)
        if not set(now) - set(leave_out):
            return AttemptResult("failed", f"Agent made no changes. Its summary: {summary}",
                                 self.branch, summary=summary)
        if not tests_passed:
            return AttemptResult("failed", f"Agent reports tests are not passing: {summary}",
                                 self.branch, summary=summary)

        repo.commit_and_push(git_dir, work_tree, self.branch, self._commit_message(summary),
                             leave_out=leave_out)
        self.heartbeat("pushed")

        # Draft-only opens the PR as a draft so it's reviewed on GitHub: merging it approves
        # the fix and closing it rejects it (see the /github/webhook endpoint).
        draft = self.playbook_run.execution_mode == ExecutionMode.DRAFT_ONLY
        pr_url = repo.open_pull_request(
            self.branch, *pr_title_body(self.playbook_run, summary), draft=draft
        )
        return AttemptResult("succeeded", "", self.branch, pr_url, summary=summary)

    def _neighbour_projects(self) -> list:
        """Service mesh: the repos of services the failing one calls or is called by. Only
        repos on this project's own GitHub App installation, so the agent never reads code
        from a GitHub account this project's repo doesn't share (and could copy it into
        this repo's PR)."""
        if not (settings.SRE_SERVICE_MESH_ENABLED and settings.SRE_MAX_NEIGHBOUR_REPOS > 0):
            return []
        service = str((self.run.root_cause or {}).get("service_name")
                      or (self.run.telemetry or {}).get("service_name") or "")
        candidates = mesh.neighbour_projects(self.project, service, limit=50)
        same_account = [p for p in candidates
                        if p.github_installation_id == self.project.github_installation_id]
        return same_account[:settings.SRE_MAX_NEIGHBOUR_REPOS]

    def _clone_neighbours(self, root: Path) -> None:
        """Best effort: a neighbour that can't be cloned is left out, never fails the fix."""
        for project in self._neighbour_projects():
            directory = f"{project.github_repo_owner}-{project.github_repo_name}"[:100]
            dest = root / directory
            try:
                root.mkdir(parents=True, exist_ok=True)
                GitHubRepo(project).clone_snapshot(dest)
            except Exception as exc:  # includes a revoked installation token
                logger.warning("could not clone neighbour %s for run %s: %s", directory,
                               self.playbook_run.id, exc)
                shutil.rmtree(dest, ignore_errors=True)
                continue
            self.neighbours[directory] = (project, dest)
            self.heartbeat(f"cloned neighbour {directory}")

    def _install_dependencies(self, box: Sandbox, commands: list[str]) -> str:
        """Returns a report for the agent: what ran and how it went."""
        lines = []
        for command in commands:
            self.heartbeat(f"installing: {command}")
            exit_code, output = box.run(command, timeout=INSTALL_TIMEOUT_SECONDS)
            status = "ok" if exit_code == 0 else f"FAILED (exit {exit_code})"
            lines.append(f"$ {command}  -> {status}")
            if exit_code != 0:
                lines.append(output[-2000:])
        return "\n".join(lines)

    def _agent_loop(self, box: Sandbox, install_report: str = "") -> tuple[str, bool]:
        client = client_for(get_llm_config(self.project, PipelineStep.PLAYBOOK_EXECUTION))
        kickoff = (
            incident_context(self.run)
            + "\n\nClassification:\n" + json.dumps(self.run.classification)
            + "\n\nPlaybook to follow:\n"
            + untrusted("playbook", {"title": self.playbook.title,
                                     "description": self.playbook.description,
                                     "steps": self.playbook.steps})
        )
        if self.runbook is not None:
            kickoff += (
                "\n\nRunbook: this fixed the same bug in this repo before. Follow it, adapting "
                "where the code has changed:\n"
                + untrusted("runbook", {"title": self.runbook.title, "area": self.runbook.area,
                                        "steps": self.runbook.steps})
            )
        if self.neighbours:
            listing = {f"{NEIGHBOURS}/{d}": f"{p.github_repo_owner}/{p.github_repo_name}"
                       for d, (p, _) in self.neighbours.items()}
            kickoff += (
                "\n\nRead-only copies of the repos of services this one calls or is called by "
                "are available for reference (read_file / list_files with these absolute paths). "
                "Use them to see how the other side uses this code; only /workspace can be "
                "changed, and only it becomes the pull request:\n" + json.dumps(listing, indent=2)
            )
        if install_report:
            kickoff += (
                "\n\nThe repo's dependencies were installed before you started"
                + (" and the sandbox now has no network, so you can't install more"
                   if settings.SRE_SANDBOX_NETWORK == "none" else "")
                + ". Use the project's environment, e.g. "
                "`uv run pytest` or `.venv/bin/python -m pytest` for Python. Install log:\n"
                + untrusted("dependency_install", install_report)
            )
        if self.previous_feedback:
            kickoff += (
                "\n\nA previous attempt at this fix failed. Take a different approach where "
                "needed. Its error output:\n" + untrusted("previous_attempt", self.previous_feedback)
            )
        jev = JevAssist.for_run(self.run)
        if jev is not None:
            kickoff += self._brief(box, jev)
        history = AgentHistory(kickoff)
        propose = settings.SRE_RUNBOOKS_ENABLED and self.runbook is None
        system = agent_system(self.playbook_run.generate_tests, propose_runbook=propose)
        env_problems = stuck_reviews = 0

        for turn in range(MAX_TURNS):
            self.heartbeat(f"turn {turn}")
            with usage_turn(turn):
                if turn and turn % COMPACT_EVERY_TURNS == 0:
                    stuck_reviews = stuck_reviews + 1 if self._review(history, jev) else 0
                    if stuck_reviews >= 2:
                        return ("Stopped early: the agent kept repeating itself without "
                                "getting closer to a fix", False)
                    if stuck_reviews:
                        history.note("Note from the platform: you seem to be going in circles. "
                                     "Change your approach, or finish with tests_passed=false "
                                     "and explain what blocks the fix.")
                reply = client.chat(system, history.messages, name=f"agent_turn_{turn}")
                try:
                    action = parse_json(reply)
                except LLMError as exc:
                    history.add_turn(reply, None,
                                     f"Invalid reply: {exc}. Reply with one JSON action.")
                    continue
                if action.get("action") == "finish":
                    if propose and isinstance(action.get("runbook"), dict):
                        self.runbook_draft = clean_runbook_draft(action["runbook"])
                    return str(action.get("summary", "")), bool(action.get("tests_passed"))
                observation = self._do(box, action)
                label = ""
                if (jev is not None and action.get("action") == "run_command"
                        and not observation.startswith("exit code 0")):
                    reading = jev.read_output(str(action.get("command")), observation)
                    if reading is not None:
                        label = reading.choice
                        confident_env = (reading.choice == "environment_problem"
                                         and reading.confidence >= ENV_PROBLEM_CONFIDENCE)
                        env_problems = env_problems + 1 if confident_env else 0
                history.add_turn(reply, action, untrusted("tool_result", observation),
                                 observation, label)
                if env_problems >= 2:
                    return ("Stopped early: the sandbox environment looks broken, not the code. "
                            "Last command output:\n" + observation[-1500:], False)
        return f"Ran out of turns after {MAX_TURNS} steps", False

    def _brief(self, box: Sandbox, jev: JevAssist) -> str:
        """Jev plays twenty questions (directory → files → function, plus the tests and
        how to run them) and the answers go in the first message, so the agent doesn't
        spend turns (each resending the whole history) exploring."""
        listing = box.source_files()
        paths = jev.source_files(listing)
        if not paths:
            return ""
        if not settings.SRE_AGENT_JEV_LOCALIZE:
            return self._file_blocks(box, jev.pick_files(jev.rank(paths), PRELOAD_MAX_FILES))
        code = [p for p in paths if not is_test_file(p)]
        tests = [p for p in paths if is_test_file(p)]
        picked = jev.pick_files(jev.narrow(code), PRELOAD_MAX_FILES)

        def read(path: str) -> str:
            try:
                return box.read_text(path)
            except SandboxError:
                return ""
        lines, location, test_file = [], None, None
        if picked:
            target = picked[0]
            if target.endswith(".py"):
                try:
                    location = jev.pick_function(target, box.read_text(target))
                except SandboxError:
                    location = None
            where = f"`{target}`"
            if location is not None:
                where += f", function `{location.name}` (lines {location.start}-{location.end})"
            lines.append(f"- Likely fix location: {where}")
            test_file = jev.pick_test_file(target, tests, read)
        lines.append(f"- Tests for it: `{test_file}`" if test_file else
                     "- No existing test file found for it" if tests else
                     "- The repo has no tests yet")

        evidence = "Files: " + ", ".join(listing[:300])
        if "pyproject.toml" in listing:
            evidence += "\n\npyproject.toml:\n" + read("pyproject.toml")
        command = jev.pick_test_command(test_command_candidates(set(listing), read), evidence)
        if command:
            lines.append(f"- Test command: `{command}`")
        self.heartbeat("localized the bug")
        brief = ("\n\nWhere to look, from the platform's localization (a cheap model made it: "
                 "check it, it can be wrong):\n" + "\n".join(lines)
                 + "\n\nThe repo's source files (no need to list_files):\n"
                 + untrusted("repo_files", "\n".join(paths[:REPO_MAP_MAX_FILES])))
        preload = picked + ([test_file] if test_file else [])
        return brief + self._file_blocks(box, preload, location)

    def _file_blocks(self, box: Sandbox, paths: list[str], location=None) -> str:
        blocks, budget = [], PRELOAD_MAX_CHARS
        for index, path in enumerate(paths):
            try:
                content = box.read_text(path)
            except SandboxError:
                continue
            if index == 0 and location is not None and len(content) > WHOLE_FILE_CHARS:
                content = function_excerpt(content, location)
                label = f"file {path} (outline + lines around {location.name})"
            else:
                label = f"file {path}"
            if len(content) > min(budget, WHOLE_FILE_CHARS * 2):
                continue
            budget -= len(content)
            blocks.append(untrusted(label, content))
        if not blocks:
            return ""
        self.heartbeat(f"preloaded {len(blocks)} files")
        return ("\n\nFiles that are likely relevant, already read for you (don't read them again "
                "unless you've changed them):\n" + "\n".join(blocks))

    def _review(self, history: AgentHistory, jev: JevAssist | None) -> bool:
        """Trims old turns (Jev, when present, also judges which old results can go) and
        returns whether Jev is confident the agent is stuck."""
        drop, progress = set(), None
        if jev is not None:
            candidates = (history.review_candidates(KEEP_RECENT_TURNS)
                          if settings.SRE_AGENT_COMPACT_HISTORY else [])
            drop, progress = jev.review(history, candidates)
        if settings.SRE_AGENT_COMPACT_HISTORY:
            saved = history.compact(KEEP_RECENT_TURNS, drop)
            logger.info("run %s: trimmed %d chars from the agent history", self.playbook_run.id, saved)
        return (progress is not None and progress.choice == "stuck"
                and progress.confidence >= STUCK_CONFIDENCE)

    def _do(self, box: Sandbox, action: dict) -> str:
        kind = action.get("action")
        try:
            if kind == "list_files":
                return box.list_files(str(action.get("path", ".")))
            if kind == "read_file":
                if action.get("start") is not None or action.get("end") is not None:
                    return box.read_file(str(action["path"]), _line(action.get("start")),
                                         _line(action.get("end")))
                return box.read_file(str(action["path"]))
            if kind == "edit_file":
                box.replace_in_file(str(action["path"]), str(action["old"]),
                                    str(action.get("new", "")))
                self.steps.append({"type": "edit_file", "path": str(action["path"])})
                return "ok"
            if kind == "write_file":
                box.write_file(str(action["path"]), str(action.get("content", "")))
                self.steps.append({"type": "edit_file", "path": str(action["path"])})
                return "ok"
            if kind == "run_command":
                exit_code, output = box.run(str(action["command"]))
                self.steps.append({"type": "run_command", "command": str(action["command"])})
                return f"exit code {exit_code}\n{output}"
        except KeyError as exc:
            return f"missing field {exc}"
        except SandboxError as exc:
            return f"error: {exc}"
        return f"unknown action {kind!r}"

    def _commit_message(self, summary: str) -> str:
        return f"fix: {self.playbook.title}\n\n{summary}\n\nIncident trace: {self.run.trace_id}"[:5000]


def _line(value) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def function_excerpt(source: str, location) -> str:
    """For a long file: its outline (every def/class line) and the located function with a
    few lines around it, instead of the whole file."""
    lines = source.splitlines()
    outline = [(n, f"{n}: {line.strip()}") for n, line in enumerate(lines, start=1)
               if line.lstrip().startswith(("def ", "async def ", "class "))]
    # The definitions nearest the function, in file order.
    nearest = sorted(outline, key=lambda item: abs(item[0] - location.start))[:OUTLINE_MAX_ENTRIES]
    outline = [text for _, text in sorted(nearest)]
    first = max(1, location.start - FUNCTION_CONTEXT_LINES)
    last = min(len(lines), location.end + FUNCTION_CONTEXT_LINES)
    return ("Outline (line: definition):\n" + "\n".join(outline)
            + f"\n\nLines {first}-{last}:\n" + "\n".join(lines[first - 1:last]))


def pr_title_body(playbook_run: PlaybookRun, summary: str) -> tuple[str, str]:
    playbook, run = playbook_run.playbook, playbook_run.incident_run
    title = f"[SRE agent] {playbook.title}"[:250]
    body = (
        f"Automated fix for incident trace `{run.trace_id}`.\n\n"
        f"**Playbook:** {playbook.title} ({playbook.status})\n\n"
        f"**Summary:**\n{summary}\n\n"
        "_Opened by the SRE agent. Review before merging._"
    )
    return title, body[:60000]
