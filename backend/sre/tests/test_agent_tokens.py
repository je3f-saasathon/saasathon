"""The agent loop's token savings: history trimming, Jev assist, and usage per turn."""

import json
from types import SimpleNamespace as NS

import pytest

from jev.client import JevError
from sre.llm import clients
from sre.llm.usage import usage_scope
from sre.models import LLMProviderConfig, LLMStepOverride, LLMUsage
from sre.services.agent_history import AgentHistory
from sre.services.context import untrusted
from sre.services.executor import PlaybookExecutor
from sre.services.jev_assist import JevAssist

from .test_activities import (  # noqa: F401 (fixtures)
    AGENT_TURNS, FakeLLM, FakeSandbox, _playbook_run, fake_infra, incident, project,
)

pytestmark = pytest.mark.django_db


# ---- history trimming ------------------------------------------------------------------

def _turn(history, action, observation):
    history.add_turn(json.dumps(action), action, untrusted("tool_result", observation), observation)


def _history():
    history = AgentHistory("KICKOFF")
    _turn(history, {"action": "read_file", "path": "db.py"}, "OLD DB CONTENT " * 50)
    _turn(history, {"action": "write_file", "path": "db.py", "content": "NEW\n" * 100}, "ok")
    _turn(history, {"action": "run_command", "command": "pytest"},
          "exit code 1\n" + "\n".join(f"line {n}" for n in range(40)))
    _turn(history, {"action": "read_file", "path": "models.py"}, "MODELS CONTENT")
    _turn(history, {"action": "run_command", "command": "pytest"}, "exit code 0\n1 passed")
    return history


def test_compact_trims_what_later_turns_replaced():
    history = _history()
    before = [m["content"] for m in history.messages]
    saved = history.compact(keep_recent=1)
    messages = [m["content"] for m in history.messages]
    assert saved > 0
    assert messages[0] == "KICKOFF"
    assert "OLD DB CONTENT" not in messages[2] and "a later turn replaced it" in messages[2]
    assert "NEW" not in messages[3] and json.loads(messages[3])["path"] == "db.py"
    # An older command keeps only its last lines; the latest command is left whole.
    assert "line 39" in messages[6] and "line 20" not in messages[6]
    assert messages[8] == before[8]  # models.py was never replaced
    assert messages[9:] == before[9:]  # the recent turn is untouched
    assert len(history.messages) == len(before)  # same turns, so roles still alternate


def test_compact_drops_results_judged_irrelevant_and_is_idempotent():
    history = _history()
    assert history.review_candidates(keep_recent=1) == [3]  # models.py, the only live read
    history.compact(keep_recent=1, drop={3})
    assert "no longer relevant" in history.messages[8]["content"]
    assert history.compact(keep_recent=1, drop={3}) == 0


# ---- the loop --------------------------------------------------------------------------

class FakeJev:
    """Stands in for Cloudflare; `answer(key, instructions)` returns (choice, probability)."""

    def __init__(self, monkeypatch, answer, fail=False):
        self.answer = answer
        self.fail = fail
        self.calls = []
        monkeypatch.setattr(clients, "run_jev", self._run)

    def _run(self, state, questions, **kwargs):
        self.calls.append((state, questions))
        if self.fail:
            raise JevError("Jev is down", 503)
        answers = {}
        for key, question in questions.items():
            choice, probability = self.answer(key, question["instructions"])
            answers[key] = {"choice": choice, "probabilities": {choice: probability}}
        return {"answers": answers, "usage": {"input_tokens": 7, "output_tokens": 1}}


def _use_jev(project):
    config = LLMProviderConfig.objects.create(owner=project.memberships.get().user, name="jev",
                                              provider="jev_cloudflare")
    LLMStepOverride.objects.create(project=project, step="playbook_similarity_judge",
                                   llm_config=config)


class RepoSandbox(FakeSandbox):
    """Lists files for the preload and fails commands in `fail`."""

    fail: dict = {}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.files = {"db.py": "POOL = 5\n", "views.py": "VIEWS\n", "README.md": "BUGS"}

    def run(self, command, timeout=None):
        self.commands.append((command, self.network))
        if command.startswith("find "):
            return 0, "\n".join(f"./{p}" for p in self.files)
        if command in self.fail:
            return self.fail[command]
        return 0, "1 passed"


@pytest.fixture
def repo_sandbox(monkeypatch, fake_infra):
    from sre.services import executor as executor_module
    RepoSandbox.fail = {}
    monkeypatch.setattr(executor_module, "Sandbox", RepoSandbox)


def test_jev_preloads_the_files_the_fix_needs(monkeypatch, repo_sandbox, project, incident):
    _use_jev(project)
    jev = FakeJev(monkeypatch, lambda key, q: ("yes", 0.9) if "db.py" in q else ("no", 0.8))
    llm = FakeLLM(monkeypatch, *AGENT_TURNS)
    result = PlaybookExecutor(_playbook_run(project, incident, "autonomous"), 1, "").execute()
    assert result.outcome == "succeeded"
    kickoff = llm.prompts[0][1][0]["content"]
    assert 'label="file db.py"' in kickoff and "POOL = 5" in kickoff
    assert "VIEWS" not in kickoff
    asked = json.dumps(jev.calls[0][1])
    assert "README.md" not in asked  # only source files are candidates (and BUGS.md never)


def test_without_jev_nothing_is_preloaded(monkeypatch, repo_sandbox, project, incident):
    jev = FakeJev(monkeypatch, lambda key, q: ("yes", 1.0))
    llm = FakeLLM(monkeypatch, *AGENT_TURNS)
    PlaybookExecutor(_playbook_run(project, incident, "autonomous"), 1, "").execute()
    assert jev.calls == []
    assert "already read for you" not in llm.prompts[0][1][0]["content"]
    assert not any(c.startswith("find ") for c, _ in RepoSandbox.instances[0].commands)


def test_a_broken_environment_ends_the_attempt_early(monkeypatch, repo_sandbox, project, incident):
    _use_jev(project)
    RepoSandbox.fail = {"pytest": (127, "sh: pytest: not found")}
    FakeJev(monkeypatch, lambda key, q: ("environment_problem", 0.95) if key == "kind"
            else ("no", 0.9))
    run_tests = {"action": "run_command", "command": "pytest"}
    llm = FakeLLM(monkeypatch, AGENT_TURNS[0], run_tests, run_tests, run_tests)
    result = PlaybookExecutor(_playbook_run(project, incident, "autonomous"), 1, "").execute()
    assert result.outcome == "failed"
    assert "environment looks broken" in result.error_output
    assert len(llm.prompts) == 3  # stopped after the second broken run, not at the fourth turn


def test_an_agent_going_in_circles_is_nudged_then_stopped(monkeypatch, fake_infra, project, incident):
    _use_jev(project)
    FakeJev(monkeypatch, lambda key, q: ("stuck", 0.9) if key == "progress" else ("keep", 0.9))
    llm = FakeLLM(monkeypatch, *[{"action": "run_command", "command": "pytest"}] * 20)
    result = PlaybookExecutor(_playbook_run(project, incident, "autonomous"), 1, "").execute()
    assert "Stopped early" in result.error_output
    assert len(llm.prompts) == 12  # nudged before turn 6, stopped before turn 12
    assert "going in circles" in llm.prompts[6][1][-1]["content"]


def test_jev_errors_never_fail_the_fix(monkeypatch, repo_sandbox, project, incident):
    _use_jev(project)
    FakeJev(monkeypatch, lambda key, q: ("yes", 1.0), fail=True)
    FakeLLM(monkeypatch, *AGENT_TURNS)
    result = PlaybookExecutor(_playbook_run(project, incident, "autonomous"), 1, "").execute()
    assert result.outcome == "succeeded"


def test_the_loop_trims_old_turns(monkeypatch, fake_infra, project, incident):
    turns = ([{"action": "write_file", "path": "db.py", "content": "BIG\n" * 500}]
             + [{"action": "run_command", "command": "pytest"}] * 5
             + [AGENT_TURNS[-1]])
    llm = FakeLLM(monkeypatch, *turns)
    result = PlaybookExecutor(_playbook_run(project, incident, "autonomous"), 1, "").execute()
    assert result.outcome == "succeeded"
    assert "BIG" in llm.prompts[5][1][1]["content"]  # before the trim at turn 6
    assert "BIG" not in llm.prompts[6][1][1]["content"]


def test_trimming_can_be_turned_off(monkeypatch, settings, fake_infra, project, incident):
    settings.SRE_AGENT_COMPACT_HISTORY = False
    turns = ([{"action": "write_file", "path": "db.py", "content": "BIG\n" * 500}]
             + [{"action": "run_command", "command": "pytest"}] * 5 + [AGENT_TURNS[-1]])
    llm = FakeLLM(monkeypatch, *turns)
    PlaybookExecutor(_playbook_run(project, incident, "autonomous"), 1, "").execute()
    assert "BIG" in llm.prompts[6][1][1]["content"]


# ---- usage per turn --------------------------------------------------------------------

def test_agent_calls_record_their_turn_and_cached_tokens(monkeypatch, fake_infra, project, incident):
    FakeLLM(monkeypatch, *AGENT_TURNS, usage={"input": 100, "cached_input": 60, "output": 5})
    with usage_scope(incident.id, "playbook_execution"):
        PlaybookExecutor(_playbook_run(project, incident, "autonomous"), 1, "").execute()
    rows = LLMUsage.objects.filter(incident_run=incident).order_by("id")
    assert [(r.turn, r.input_tokens, r.cached_input_tokens) for r in rows] == [
        (0, 100, 60), (1, 100, 60), (2, 100, 60)]


def test_openai_cached_tokens_are_read_from_the_response(monkeypatch, project):
    import openai

    class CachedOpenAI:
        def __init__(self, **kwargs):
            self.chat = NS(completions=NS(create=lambda **kw: NS(
                choices=[NS(message=NS(content="hi"))],
                usage=NS(prompt_tokens=100, completion_tokens=5,
                         prompt_tokens_details=NS(cached_tokens=64)))))

    monkeypatch.setattr(openai, "OpenAI", CachedOpenAI)
    config = LLMProviderConfig(name="c", provider="openai", model="m")
    _, usage = clients.OpenAICompatibleClient(config)._chat("s", [])
    assert usage == {"input": 100, "output": 5, "cached_input": 64}


def test_jev_assist_is_off_by_setting(settings, project, incident):
    _use_jev(project)
    assert JevAssist.for_run(incident) is not None
    settings.SRE_AGENT_JEV_ASSIST = False
    assert JevAssist.for_run(incident) is None
