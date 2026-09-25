from datetime import timedelta

import pytest
from django.utils import timezone

from sre import activities
from sre.crypto import encrypt
from sre.llm.clients import OpenAICompatibleClient
from sre.llm.resolve import NoLLMConfigError, get_llm_config
from sre.models import IncidentRun, LLMProviderConfig, LLMUsage
from sre.temporal_types import IncidentInput

pytestmark = pytest.mark.django_db


@pytest.fixture
def company(settings):
    """A server with the company default configured, Jev included."""
    settings.SRE_PLATFORM_OPENAI_API_KEY = "sk-company"
    settings.SRE_PLATFORM_FAST_MODEL = "gpt-fast"
    settings.SRE_PLATFORM_STRONG_MODEL = "gpt-strong"
    settings.SRE_PLATFORM_TRIAGE_USES_JEV = True
    settings.CLOUDFLARE_ACCOUNT_ID = "acct"
    settings.CLOUDFLARE_API_TOKEN = "cf-token"
    settings.CLOUDFLARE_JEV_MODEL = "typesafe/jev"
    settings.SRE_PLATFORM_MONTHLY_TOKEN_CAP = 1000
    return settings


@pytest.fixture
def project(make_user, make_project):
    return make_project(make_user())  # no default config, no overrides


def usage(project, tokens, billed_to="platform", when=None):
    run = IncidentRun.objects.create(project=project, trace_id=f"t{LLMUsage.objects.count()}",
                                     temporal_workflow_id=f"w{LLMUsage.objects.count()}")
    row = LLMUsage.objects.create(incident_run=run, step="bug_classification", model="m",
                                  billed_to=billed_to, input_tokens=tokens, output_tokens=0)
    if when:
        LLMUsage.objects.filter(id=row.id).update(created_at=when)
    return row


def test_without_company_default_a_bare_project_still_fails_clearly(settings, project):
    settings.SRE_PLATFORM_OPENAI_API_KEY = ""
    with pytest.raises(NoLLMConfigError, match="no LLM config"):
        get_llm_config(project, "bug_classification")


def test_bare_project_uses_jev_for_triage_and_the_strong_model_for_playbooks(company, project):
    triage = get_llm_config(project, "playbook_similarity_judge")
    assert (triage.provider, triage.model, triage.is_platform) == ("jev_cloudflare", "typesafe/jev", True)
    assert not triage.api_key_encrypted  # runs on the server's Cloudflare credentials
    for step in ("playbook_creation", "playbook_execution"):
        config = get_llm_config(project, step)
        assert (config.provider, config.model, config.is_platform) == ("openai", "gpt-strong", True)
    assert config.pk is None  # never saved


def test_triage_falls_back_to_the_fast_model_without_jev(company, project):
    company.SRE_PLATFORM_TRIAGE_USES_JEV = False
    assert get_llm_config(project, "bug_classification").model == "gpt-fast"
    company.SRE_PLATFORM_TRIAGE_USES_JEV = True
    company.CLOUDFLARE_API_TOKEN = ""
    assert get_llm_config(project, "bug_classification").model == "gpt-fast"


def test_a_projects_own_config_wins_over_the_company_default(company, project):
    own = LLMProviderConfig.objects.create(owner=project.memberships.get().user, name="mine",
                                           provider="anthropic", model="claude-x",
                                           api_key_encrypted=encrypt("k"))
    project.default_llm_config = own
    project.save()
    assert get_llm_config(project, "playbook_execution") == own


def test_company_calls_are_tagged_as_ours(monkeypatch, company, project):
    company.SRE_PLATFORM_TRIAGE_USES_JEV = False
    monkeypatch.setattr(OpenAICompatibleClient, "_chat",
                        lambda self, system, messages: ('{"is_anomaly": true, "reasoning": "r"}',
                                                        {"input": 40, "output": 2}))
    run = IncidentRun.objects.create(project=project, trace_id="t", temporal_workflow_id="w",
                                     raw_webhook_payload={"payload": {}})
    activities.confirm_anomaly(IncidentInput(run.id, project.id))
    row = LLMUsage.objects.get(incident_run=run)
    assert (row.billed_to, row.model, row.input_tokens) == ("platform", "gpt-fast", 40)


def test_monthly_cap_stops_company_usage_with_a_clear_message(company, project):
    usage(project, 999)
    get_llm_config(project, "playbook_execution")  # under the cap
    usage(project, 1)
    with pytest.raises(NoLLMConfigError, match="Settings → Models"):
        get_llm_config(project, "playbook_execution")


def test_cap_ignores_last_month_and_the_users_own_keys(company, project):
    usage(project, 5000, when=timezone.now().replace(day=1) - timedelta(days=2))
    usage(project, 5000, billed_to="user")
    get_llm_config(project, "playbook_execution")  # still allowed


def test_cap_never_blocks_a_project_on_its_own_key(company, project):
    usage(project, 5000)
    own = LLMProviderConfig.objects.create(owner=project.memberships.get().user, name="mine",
                                           provider="openai", model="gpt-x",
                                           api_key_encrypted=encrypt("k"))
    project.default_llm_config = own
    project.save()
    assert get_llm_config(project, "playbook_execution") == own


def test_a_users_keyless_jev_config_bills_us_and_is_capped(company, project):
    keyless = LLMProviderConfig.objects.create(owner=project.memberships.get().user,
                                               name="jev", provider="jev_cloudflare")
    project.default_llm_config = keyless
    project.save()
    get_llm_config(project, "bug_classification")
    usage(project, 1000)
    with pytest.raises(NoLLMConfigError, match="company model"):
        get_llm_config(project, "bug_classification")


def test_platform_endpoint_and_project_usage(api_for, company, project):
    api = api_for(project.memberships.get().user)
    assert api.get("/platform").json() == {"available": True, "triage_model": "jev",
                                           "strong_model": "gpt-strong", "monthly_token_cap": 1000}
    usage(project, 300)
    usage(project, 50, billed_to="user")
    assert api.get(f"/projects/{project.id}").json()["platform_tokens_this_month"] == 300
    runs = api.get("/incident-runs").json()["runs"]
    assert sorted(r["usage"]["platform_tokens"] for r in runs) == [0, 300]
    assert {s["billed_to"] for r in runs for s in r["usage"]["by_step"]} == {"platform", "user"}


def test_platform_endpoint_when_not_configured(api_for, settings, project):
    settings.SRE_PLATFORM_OPENAI_API_KEY = ""
    body = api_for(project.memberships.get().user).get("/platform").json()
    assert body["available"] is False and body["strong_model"] == ""
