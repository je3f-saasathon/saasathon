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

    listed = False

    def source_files(self):
        RepoSandbox.listed = True
        return list(self.files)

    def run(self, command, timeout=None):
        self.commands.append((command, self.network))
        if command in self.fail:
            return self.fail[command]
        return 0, "1 passed"


@pytest.fixture
def repo_sandbox(monkeypatch, fake_infra):
    from sre.services import executor as executor_module
    RepoSandbox.fail = {}
    RepoSandbox.listed = False
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
    assert not RepoSandbox.listed


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


# ---- twenty questions: where the bug is --------------------------------------------------

from sre.services.executor import function_excerpt  # noqa: E402
from sre.services.jev_assist import python_functions  # noqa: E402
from sre.services.jev_assist import test_command_candidates as command_candidates  # noqa: E402

VIEWS = ("import json\n\n\n"
         + "".join(f"def helper_{n}(x):\n    return x + {n}\n\n\n" for n in range(400))
         + "def create_order(request):\n    customer = None\n"
           "    discount = 10 if customer.loyalty_tier == 'gold' else 0\n    return discount\n")


class LocalizeSandbox(RepoSandbox):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.files = {"catalog/views.py": VIEWS, "catalog/tests.py": "class T: pass\n",
                      "catalog/models.py": "class Order: pass\n", "manage.py": "",
                      "pyproject.toml": "[dependency-groups]\ndev = ['pytest-django']\n",
                      "uv.lock": ""}


def _localizing_jev(key, question):
    if key == "function":
        return "create_order", 0.9
    if key == "tests":
        return "catalog/tests.py", 0.9
    if key == "command":
        return "uv run python manage.py test", 0.8
    return ("yes", 0.9) if "views.py" in question else ("no", 0.9)


def test_the_brief_names_the_function_tests_and_command(monkeypatch, fake_infra, project, incident):
    from sre.services import executor as executor_module
    monkeypatch.setattr(executor_module, "Sandbox", LocalizeSandbox)
    incident.telemetry = {"stacktrace": 'File "/app/catalog/views.py", line 1606, in create_order'}
    incident.save()
    _use_jev(project)
    FakeJev(monkeypatch, _localizing_jev)
    llm = FakeLLM(monkeypatch, *AGENT_TURNS)
    PlaybookExecutor(_playbook_run(project, incident, "autonomous"), 1, "").execute()
    kickoff = llm.prompts[0][1][0]["content"]
    assert "`catalog/views.py`, function `create_order` (lines 1604-1607)" in kickoff
    assert "Tests for it: `catalog/tests.py`" in kickoff
    assert "Test command: `uv run python manage.py test`" in kickoff
    assert 'label="repo_files"' in kickoff and "catalog/models.py" in kickoff
    # The long file goes in as an outline plus the function, not whole.
    assert "outline + lines around create_order" in kickoff and "loyalty_tier" in kickoff
    assert "return x + 200" not in kickoff
    assert 'label="file catalog/tests.py"' in kickoff


def test_localization_can_be_turned_off(monkeypatch, settings, fake_infra, project, incident):
    from sre.services import executor as executor_module
    settings.SRE_AGENT_JEV_LOCALIZE = False
    monkeypatch.setattr(executor_module, "Sandbox", LocalizeSandbox)
    _use_jev(project)
    jev = FakeJev(monkeypatch, _localizing_jev)
    llm = FakeLLM(monkeypatch, *AGENT_TURNS)
    PlaybookExecutor(_playbook_run(project, incident, "autonomous"), 1, "").execute()
    assert "Where to look" not in llm.prompts[0][1][0]["content"]
    assert [list(q) for _, q in jev.calls][0][0] == "f0"  # only the file question


def test_narrowing_walks_down_the_directory_tree(project, incident):
    _use_jev(project)
    incident.telemetry = {"stacktrace": 'File "/app/billing/invoice.py", line 3'}
    assist = JevAssist.for_run(incident)
    paths = (["billing/invoice.py"] + [f"billing/api/v{n}.py" for n in range(20)]
             + [f"shop/m{n}.py" for n in range(40)] + [f"admin/a{n}.py" for n in range(40)])
    asked = []

    def choose_many(state, questions, name):
        asked.append(questions["where"][1])
        choice = "billing" if "billing" in questions["where"][1] else "api"
        return {"where": clients.JevChoice(choice, 0.9)}

    assist.client = NS(choose_many=choose_many)
    candidates = assist.narrow(paths)
    assert asked and set(asked[0]) == {"billing", "shop", "admin"}
    assert candidates[0] == "billing/invoice.py"  # named in the stack trace: always kept
    assert not any(c.startswith(("shop/", "admin/")) for c in candidates)


def test_python_functions_and_excerpt():
    functions = python_functions("class A:\n    def m(self, x):\n        pass\n\ndef f():\n    pass\n")
    assert [(f.name, f.start, f.end) for f in functions] == [("A.m", 2, 3), ("f", 5, 6)]
    assert python_functions("def broken(:\n") == []
    excerpt = function_excerpt(VIEWS, python_functions(VIEWS)[-1])
    assert "1604: def create_order(request):" in excerpt and "loyalty_tier" in excerpt
    assert len(excerpt) < len(VIEWS) / 2


def test_test_command_candidates():
    files = {"pyproject.toml", "uv.lock", "manage.py"}
    read = {"pyproject.toml": "pytest"}.get
    assert command_candidates(files, lambda f: read(f, "")) == [
        "uv run python -m pytest -q", "uv run python manage.py test"]
    assert command_candidates({"requirements.txt"}, lambda f: "pytest==8") == [
        ".venv/bin/python -m pytest -q"]
    assert command_candidates({"package.json"}, lambda f: '{"scripts": {"test": "x"}}') == [
        "npm test"]


# ---- edit_file and ranged reads ---------------------------------------------------------

def test_the_agent_edits_with_search_and_replace(monkeypatch, repo_sandbox, project, incident):
    turns = [{"action": "edit_file", "path": "db.py", "old": "POOL = 5", "new": "POOL = 20"},
             {"action": "edit_file", "path": "db.py", "old": "missing", "new": "x"},
             {"action": "read_file", "path": "db.py", "start": 1, "end": 1},
             AGENT_TURNS[-1]]
    llm = FakeLLM(monkeypatch, *turns)
    playbook_run = _playbook_run(project, incident, "autonomous")
    result = PlaybookExecutor(playbook_run, 1, "").execute()
    assert result.outcome == "succeeded"
    assert RepoSandbox.instances[0].files["db.py"] == "POOL = 20\n"
    results = [m["content"] for m in llm.prompts[-1][1][2::2]]
    assert "ok" in results[0] and "0 times" in results[1] and "POOL = 20" in results[2]
    assert playbook_run.attempts.get().generated_steps == [{"type": "edit_file", "path": "db.py"}]


def test_edits_and_full_reads_make_older_reads_stale():
    history = AgentHistory("k")
    _turn(history, {"action": "read_file", "path": "a.py"}, "A" * 100)
    _turn(history, {"action": "read_file", "path": "b.py", "start": 1, "end": 5}, "B" * 100)
    _turn(history, {"action": "read_file", "path": "b.py", "start": 6, "end": 9}, "C" * 100)
    _turn(history, {"action": "edit_file", "path": "a.py", "old": "x" * 400, "new": "y" * 400}, "ok")
    _turn(history, {"action": "run_command", "command": "pytest"}, "exit code 0")
    history.compact(keep_recent=1)
    assert "a later turn replaced it" in history.messages[2]["content"]  # edited afterwards
    assert history.messages[4]["content"].count("B") == 100  # another range doesn't replace it
    assert "x" * 400 not in history.messages[7]["content"]  # the old edit's text is stubbed


def test_the_test_file_next_to_the_code_needs_no_question(project, incident):
    _use_jev(project)
    assist = JevAssist.for_run(incident)
    assist.client = NS(choose_many=lambda *a, **k: pytest.fail("Jev shouldn't be asked"))
    assert assist.pick_test_file("catalog/views.py", ["catalog/tests.py", "x/tests.py"]) == \
        "catalog/tests.py"
    assert assist.pick_test_file("app/views.py", ["tests/test_views.py"]) == "tests/test_views.py"
