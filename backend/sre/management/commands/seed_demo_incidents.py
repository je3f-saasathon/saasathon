from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from accounts.models import User
from sre.models import (
    ExecutionMode,
    IncidentRun,
    LLMUsage,
    Playbook,
    PlaybookExecutionAttempt,
    PlaybookRun,
    Project,
    ProjectMembership,
    ProjectRole,
)
from sre.orgs import personal_org

# Seeded projects are found (and replaced) by this name prefix.
PREFIX = "[demo] "

PROJECTS = [
    ("checkout-api", "acme", "checkout-api"),
    ("web-frontend", "acme", "web-frontend"),
]

INCIDENTS = [
    # (project index, status, category, severity, summary, playbook run status, mode, hours ago)
    (0, "succeeded", "database", "high",
     "Connection pool exhausted under load; checkout requests time out waiting for a DB connection.",
     "succeeded", ExecutionMode.AUTONOMOUS, 2),
    (0, "awaiting_approval", "null_reference", "medium",
     "Order.discount is None for legacy orders, crashing total calculation.",
     "pending_approval", ExecutionMode.DRAFT_ONLY, 5),
    (1, "failed", "dependency_failure", "high",
     "Payment provider returns 502; retries aren't bounded and exhaust the worker pool.",
     "failed", ExecutionMode.AUTONOMOUS, 9),
    (1, "advisory_complete", "configuration", "low",
     "SENTRY_DSN missing in staging, so client errors aren't reported.",
     None, ExecutionMode.ADVISORY_ONLY, 20),
    (0, "no_anomaly", "", "", "", None, None, 26),
    (1, "running", "", "", "", None, None, 0),
    (0, "new_playbook_created", "timeout", "medium",
     "Inventory lookup exceeds 30s for carts with >50 items; N+1 query per line item.",
     None, None, 40),
    (1, "succeeded", "validation", "low",
     "Signup form accepts emails with trailing spaces, creating duplicate accounts.",
     "succeeded", ExecutionMode.AUTONOMOUS, 55),
    (0, "failed", "resource_exhaustion", "high",
     "Report export loads the whole table into memory; worker OOM-killed.",
     "failed", ExecutionMode.DRAFT_ONLY, 72),
    (1, "succeeded", "logic_error", "medium",
     "Timezone offset applied twice when rendering order dates.",
     "succeeded", ExecutionMode.AUTONOMOUS, 96),
    (0, "rejected", "validation", "medium",
     "Coupon codes are compared case-sensitively, so 'SAVE10' and 'save10' differ.",
     "rejected", ExecutionMode.DRAFT_ONLY, 30),
]

LONG_DIAGNOSIS = "\n\n".join(
    f"Step {i}: Traced the failing request through the handler, the ORM call and the "
    "connection pool. The pool size is 5 while the worker count is 16, so under load most "
    "requests wait for a connection until the 30s timeout fires." for i in range(1, 9)
)


class Command(BaseCommand):
    help = "Seeds fake projects and incidents for the dashboard (DEBUG only). Re-running replaces them."

    def add_arguments(self, parser):
        parser.add_argument("--email", help="Only add this user to the demo projects (default: every user)")
        parser.add_argument("--clear", action="store_true", help="Remove the demo data and stop")

    @transaction.atomic
    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("seed_demo_incidents can only run when DEBUG=true")

        deleted, _ = Project.objects.filter(name__startswith=PREFIX).delete()
        if options["clear"]:
            self.stdout.write(f"Removed demo data ({deleted} rows).")
            return

        users = User.objects.filter(email=options["email"]) if options["email"] else User.objects.all()
        if not users:
            raise CommandError("No users to attach the demo projects to; log in once first.")

        projects = []
        for name, owner, repo in PROJECTS:
            project = Project.objects.create(
                name=PREFIX + name, github_installation_id="0",
                github_repo_owner=owner, github_repo_name=repo,
                organization=personal_org(users[0]),
            )
            for user in users:
                ProjectMembership.objects.create(project=project, user=user, role=ProjectRole.OWNER)
            projects.append(project)

        now = timezone.now()
        for n, (pi, status, category, severity, summary, pr_status, mode, hours) in enumerate(INCIDENTS, 1):
            project = projects[pi]
            run = IncidentRun.objects.create(
                project=project,
                trace_id=f"demo-trace-{n:03d}",
                temporal_workflow_id=f"demo-{project.id}-{n}",
                status=status,
                raw_webhook_payload={"demo": True},
                classification=None if not category else {
                    "anomaly": {"is_anomaly": True, "reasoning": "Error rate is 40x the 7-day baseline."},
                    "category": category, "severity": severity, "summary": summary,
                    "keywords": [category], "suspected_files": [f"src/{category}.py", "src/settings.py"],
                },
                diagnosis_report=LONG_DIAGNOSIS if n == 1 else "",
                error_message="Sandbox tests failed after 3 attempts." if status == "failed" else "",
            )
            if status == "no_anomaly":
                run.classification = {"anomaly": {"is_anomaly": False, "reasoning": "Health-check bot traffic."}}
                run.save(update_fields=["classification"])

            if category:
                playbook = Playbook.objects.create(
                    project=project,
                    title=summary.split(";")[0].split(",")[0][:80],
                    description="Seeded demo playbook.",
                    keywords=[category],
                    steps=[
                        {"type": "read_file", "path": f"src/{category}.py", "instructions": "Find the failing code path."},
                        {"type": "edit_file", "path": f"src/{category}.py", "instructions": "Apply the fix."},
                        {"type": "run_tests", "instructions": "Run the affected test module."},
                    ],
                    status=Playbook.Status.CONFIRMED if pr_status == "succeeded" else Playbook.Status.UNCONFIRMED,
                    source_incident_run=run if status == "new_playbook_created" else None,
                )
                if status != "new_playbook_created":
                    run.matched_playbook = playbook
                    run.save(update_fields=["matched_playbook"])

                if pr_status:
                    playbook_run = PlaybookRun.objects.create(
                        incident_run=run, playbook=playbook, execution_mode=mode, status=pr_status,
                        branch_name=f"sre/fix-{n}",
                        pr_url=f"https://github.com/{project.github_repo_owner}/{project.github_repo_name}/pull/{100 + n}"
                        # Draft-only runs keep their PR link while awaiting review or once rejected.
                        if pr_status in ("succeeded", "pending_approval", "rejected") else "",
                        # A rejection records when the PR was closed (the reopen window starts then).
                        approved_at=now - timedelta(hours=hours - 1) if pr_status == "rejected" else None,
                    )
                    tries = 3 if pr_status == "failed" else 1
                    for a in range(1, tries + 1):
                        ok = pr_status == "succeeded" or (pr_status != "failed" and a == tries)
                        PlaybookExecutionAttempt.objects.create(
                            playbook_run=playbook_run, attempt_number=a,
                            generated_steps=playbook.steps,
                            outcome="succeeded" if ok else ("failed" if pr_status == "failed" else "pending"),
                            summary=f"Attempt {a}: patched src/{category}.py and ran the tests.",
                            error_output="" if ok else "AssertionError: expected 200, got 500",
                            branch_name=f"sre/fix-{n}",
                        )

            for step, inp, out in [("anomaly_double_check", 1800, 120), ("bug_classification", 2400, 300),
                                   ("playbook_execution", 12000, 2500)][: 1 if not category else 3]:
                LLMUsage.objects.create(
                    incident_run=run, step=step, provider="anthropic", model="claude-sonnet-5",
                    billed_to="platform" if n % 2 else "user", input_tokens=inp * n, output_tokens=out * n,
                )

            # created_at is auto_now_add, so backdate with update().
            IncidentRun.objects.filter(pk=run.pk).update(created_at=now - timedelta(hours=hours))

        self.stdout.write(self.style.SUCCESS(
            f"Seeded {len(projects)} projects and {len(INCIDENTS)} incidents for "
            f"{', '.join(u.email for u in users)}."
        ))
