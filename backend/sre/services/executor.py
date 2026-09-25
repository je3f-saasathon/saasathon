import json
import shutil
from pathlib import Path
from typing import Callable

from django.conf import settings

from ..llm.clients import LLMError, client_for, parse_json
from ..llm.resolve import get_llm_config
from ..models import ExecutionMode, PipelineStep, PlaybookExecutionAttempt, PlaybookRun
from ..temporal_types import AttemptResult
from .context import UNTRUSTED_NOTICE, incident_context, untrusted
from .github import GitError, GitHubRepo
from .sandbox import Sandbox, SandboxError

MAX_TURNS = 40

AGENT_SYSTEM = (
    "You are an SRE agent fixing a production bug in a git repository checked out at "
    "/workspace. Follow the playbook, adapting it to this incident. Work in small steps: "
    "inspect, edit, then run the relevant tests. Add or update a test that covers the fix "
    "when the repo has tests. " + UNTRUSTED_NOTICE + "\n\n"
    "Each reply must be exactly one JSON object choosing one action:\n"
    '{"action": "list_files", "path": "."}\n'
    '{"action": "read_file", "path": "src/app.py"}\n'
    '{"action": "write_file", "path": "src/app.py", "content": "<full new file content>"}\n'
    '{"action": "run_command", "command": "pytest -x tests/test_app.py"}\n'
    '{"action": "finish", "summary": "<what you changed and why>", "tests_passed": true|false}\n'
    "Paths are relative to /workspace. You cannot commit, push or open pull requests; "
    "that happens after you finish. Call finish with tests_passed=false if you could not "
    "get the tests passing."
)


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
        self.attempt_number = attempt_number
        self.previous_feedback = previous_feedback
        self.heartbeat = heartbeat
        self.branch = branch_name_for(playbook_run, attempt_number)
        self.steps: list[dict] = []

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

        with Sandbox(work_tree, name=f"sre-{self.playbook_run.id}-a{self.attempt_number}") as box:
            summary, tests_passed = self._agent_loop(box)

        if not repo.has_changes(git_dir, work_tree):
            return AttemptResult("failed", f"Agent made no changes. Its summary: {summary}",
                                 self.branch, summary=summary)
        if not tests_passed:
            return AttemptResult("failed", f"Agent reports tests are not passing: {summary}",
                                 self.branch, summary=summary)

        repo.commit_and_push(git_dir, work_tree, self.branch, self._commit_message(summary))
        self.heartbeat("pushed")

        # Draft-only still opens the PR (as a draft) so it can be reviewed on GitHub;
        # approval in our app is what makes it ready for review.
        draft = self.playbook_run.execution_mode == ExecutionMode.DRAFT_ONLY
        pr_url = repo.open_pull_request(
            self.branch, *pr_title_body(self.playbook_run, summary), draft=draft
        )
        return AttemptResult("succeeded", "", self.branch, pr_url, summary=summary)

    def _agent_loop(self, box: Sandbox) -> tuple[str, bool]:
        client = client_for(get_llm_config(self.project, PipelineStep.PLAYBOOK_EXECUTION))
        kickoff = (
            incident_context(self.run)
            + "\n\nClassification:\n" + json.dumps(self.run.classification)
            + "\n\nPlaybook to follow:\n"
            + untrusted("playbook", {"title": self.playbook.title,
                                     "description": self.playbook.description,
                                     "steps": self.playbook.steps})
        )
        if self.previous_feedback:
            kickoff += (
                "\n\nA previous attempt at this fix failed. Take a different approach where "
                "needed. Its error output:\n" + untrusted("previous_attempt", self.previous_feedback)
            )
        messages = [{"role": "user", "content": kickoff}]

        for turn in range(MAX_TURNS):
            self.heartbeat(f"turn {turn}")
            reply = client.chat(AGENT_SYSTEM, messages, name=f"agent_turn_{turn}")
            messages.append({"role": "assistant", "content": reply})
            try:
                action = parse_json(reply)
            except LLMError as exc:
                messages.append({"role": "user", "content": f"Invalid reply: {exc}. Reply with one JSON action."})
                continue
            if action.get("action") == "finish":
                return str(action.get("summary", "")), bool(action.get("tests_passed"))
            observation = self._do(box, action)
            messages.append({"role": "user", "content": untrusted("tool_result", observation)})
        return f"Ran out of turns after {MAX_TURNS} steps", False

    def _do(self, box: Sandbox, action: dict) -> str:
        kind = action.get("action")
        try:
            if kind == "list_files":
                return box.list_files(str(action.get("path", ".")))
            if kind == "read_file":
                return box.read_file(str(action["path"]))
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
