import pytest
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from sre.models import Organization, Playbook
from sre.playbook_library import BUILTIN_PLAYBOOKS
from sre.services.triage import CATEGORIES

pytestmark = pytest.mark.django_db

GENERIC = [{"type": "investigate", "instructions": "find it"},
           {"type": "change", "instructions": "fix it"},
           {"type": "verify", "instructions": "test it"}]


@pytest.fixture
def runbooks_on(settings):
    settings.SRE_RUNBOOKS_ENABLED = True


@pytest.fixture
def world(make_user, make_project, add_member):
    """Two projects in one owner's org, one in another org, and a playbook of each kind."""
    owner, admin, viewer, stranger = (make_user(e) for e in
                                      ("o@x.com", "a@x.com", "v@x.com", "s@x.com"))
    shop = make_project(owner, name="shop")
    blog = make_project(owner, name="blog")
    add_member(shop, admin, "admin")
    add_member(shop, viewer, "viewer")
    elsewhere = make_project(stranger, name="elsewhere")
    org = shop.organization
    return {
        "owner": owner, "admin": admin, "viewer": viewer, "stranger": stranger,
        "shop": shop, "blog": blog, "elsewhere": elsewhere,
        "generic": Playbook.objects.create(organization=org, project=blog, is_generic=True,
                                           title="org generic", steps=GENERIC),
        "shop_legacy": Playbook.objects.create(organization=org, project=shop, title="shop legacy"),
        "blog_legacy": Playbook.objects.create(organization=org, project=blog, title="blog legacy"),
        "other_org": Playbook.objects.create(organization=elsewhere.organization, is_generic=True,
                                             title="other org generic"),
        "builtin": Playbook.objects.get(slug="null-reference"),
    }


def _titles(api, project):
    return {p["title"] for p in api.get(f"/projects/{project.id}/playbooks").json()["playbooks"]}


# ---- built-ins ----------------------------------------------------------------------

def test_builtins_cover_every_category_and_are_generic():
    builtins = Playbook.objects.filter(origin="builtin")
    assert builtins.count() == len(BUILTIN_PLAYBOOKS)
    assert {p.category for p in builtins} == set(CATEGORIES)
    for playbook in builtins:
        assert playbook.organization_id is None and playbook.project_id is None
        assert playbook.is_generic and playbook.status == "confirmed"
        assert {s["type"] for s in playbook.steps} == {"investigate", "change", "verify"}
        assert all("path" not in s for s in playbook.steps)


def test_seed_playbooks_is_idempotent_and_restores_edits():
    Playbook.objects.filter(slug="timeout").update(title="edited")
    call_command("seed_playbooks")
    call_command("seed_playbooks")
    assert Playbook.objects.filter(origin="builtin").count() == len(BUILTIN_PLAYBOOKS)
    assert Playbook.objects.get(slug="timeout").title == "Slow call or timeout"


# ---- visibility -----------------------------------------------------------------------

def test_runbooks_off_lists_only_the_projects_own(world, api_for):
    assert _titles(api_for(world["owner"]), world["shop"]) == {"shop legacy"}
    assert api_for(world["owner"]).get(f"/playbooks/{world['builtin'].id}").status_code == 404


def test_runbooks_on_lists_builtins_org_generic_and_own_legacy(world, api_for, runbooks_on):
    titles = _titles(api_for(world["viewer"]), world["shop"])
    builtin_titles = {p["title"] for p in BUILTIN_PLAYBOOKS}
    assert titles == builtin_titles | {"org generic", "shop legacy"}
    # Not another project's legacy playbook (its file paths stay private), not another org's.
    assert "blog legacy" not in titles and "other org generic" not in titles


def test_reading_follows_visibility(world, api_for, runbooks_on):
    viewer = api_for(world["viewer"])
    assert viewer.get(f"/playbooks/{world['builtin'].id}").status_code == 200
    assert viewer.get(f"/playbooks/{world['generic'].id}").status_code == 200
    assert viewer.get(f"/playbooks/{world['shop_legacy'].id}").status_code == 200
    assert viewer.get(f"/playbooks/{world['blog_legacy'].id}").status_code == 404
    assert viewer.get(f"/playbooks/{world['other_org'].id}").status_code == 404


# ---- writing ----------------------------------------------------------------------------

def test_builtins_are_read_only(world, api_for, runbooks_on):
    owner = api_for(world["owner"])
    assert owner.patch(f"/playbooks/{world['builtin'].id}", {"title": "x"}).status_code == 403
    assert owner.delete(f"/playbooks/{world['builtin'].id}").status_code == 403


def test_project_admin_in_the_org_can_edit_org_playbooks(world, api_for, runbooks_on):
    # admin is only an admin on shop, and not an org member, but the playbook is org-wide.
    resp = api_for(world["admin"]).patch(f"/playbooks/{world['generic'].id}", {"title": "better"})
    assert resp.status_code == 200 and resp.json()["title"] == "better"
    assert api_for(world["viewer"]).patch(f"/playbooks/{world['generic'].id}",
                                          {"title": "x"}).status_code == 403
    assert api_for(world["stranger"]).patch(f"/playbooks/{world['generic'].id}",
                                            {"title": "x"}).status_code == 404


def test_project_create_makes_an_org_playbook_when_on(world, api_for, runbooks_on):
    body = {"title": "t", "steps": GENERIC + [{"type": "create_pr"}], "category": "timeout"}
    resp = api_for(world["admin"]).post(f"/projects/{world['shop'].id}/playbooks", body)
    assert resp.status_code == 201
    playbook = resp.json()
    assert playbook["is_generic"] and playbook["organization_id"] == world["shop"].organization_id
    assert playbook["origin"] == "human" and playbook["created_by_id"] == world["admin"].id
    assert [s["type"] for s in playbook["steps"]] == ["investigate", "change", "verify"]
    assert "t" in _titles(api_for(world["owner"]), world["blog"])  # visible org-wide


def test_unknown_category_is_rejected(world, api_for):
    resp = api_for(world["owner"]).post(f"/projects/{world['shop'].id}/playbooks",
                                        {"title": "t", "category": "nope"})
    assert resp.status_code == 400


def test_org_playbook_endpoint_needs_org_admin(world, api_for, make_user):
    org = Organization.objects.create(name="Acme")
    member = make_user("m@x.com")
    org.memberships.create(user=world["owner"], role="owner")
    org.memberships.create(user=member, role="member")
    assert api_for(member).post(f"/organizations/{org.id}/playbooks", {"title": "t"}).status_code == 403
    resp = api_for(world["owner"]).post(f"/organizations/{org.id}/playbooks", {"title": "t"})
    assert resp.status_code == 201 and resp.json()["project_id"] is None


# ---- migrations -------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
def test_sre_migrations_round_trip():
    executor = MigrationExecutor(connection)
    executor.migrate([("sre", "0005_llmusage_billed_to")])
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
    assert Playbook.objects.filter(origin="builtin").count() == len(BUILTIN_PLAYBOOKS)
