from django.core.management.base import BaseCommand

from sre.models import Playbook
from sre.services import generalize as g


class Command(BaseCommand):
    help = ("Turns old, specific playbooks into a generic playbook plus a runbook for their "
            "project. Dry run by default; --apply writes, --revert undoes, --merge also folds "
            "duplicates into an existing generic playbook (archived, never deleted).")

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Make the changes (uses the LLM)")
        parser.add_argument("--revert", action="store_true", help="Undo a previous --apply")
        parser.add_argument("--merge", action="store_true", help="With --apply: fold duplicates")
        parser.add_argument("--project", type=int, help="Only this project's playbooks")

    def handle(self, *args, **options):
        if options["revert"]:
            return self._revert(options["project"])
        targets = list(g.legacy_playbooks(options["project"]))
        done, skipped, failed = [], [], []
        for playbook in targets:
            reason = g.skip_reason(playbook)
            if reason:
                skipped.append(playbook)
                self.stdout.write(f"skip   {g.describe(playbook)}: {reason}")
                continue
            if not options["apply"]:
                self.stdout.write(f"would  {g.describe(playbook)}: copy into a runbook, rewrite as generic")
                continue
            try:
                runbook = g.generalize(playbook)
            except Exception as exc:  # one bad rewrite must not stop the rest
                failed.append(playbook)
                self.stderr.write(f"failed {g.describe(playbook)}: {exc}")
                continue
            done.append(playbook)
            self.stdout.write(f"done   playbook {playbook.id} -> '{playbook.title}', runbook {runbook.id}")
            if options["merge"]:
                target = g.merge(playbook)
                if target is not None:
                    self.stdout.write(f"merged playbook {playbook.id} into {target.id} '{target.title}'")
        mode = "applied" if options["apply"] else "dry run"
        self.stdout.write(f"{mode}: {len(targets)} legacy, {len(done)} generalized, "
                          f"{len(skipped)} skipped, {len(failed)} failed")

    def _revert(self, project_id):
        qs = Playbook.objects.filter(is_generic=True).exclude(legacy_snapshot={})
        if project_id is not None:
            qs = qs.filter(project_id=project_id)
        count = 0
        for playbook in qs.order_by("-id"):
            if "title" in playbook.legacy_snapshot:  # generalized by this command
                g.revert(playbook)
                count += 1
                self.stdout.write(f"reverted playbook {playbook.id} -> '{playbook.title}'")
        self.stdout.write(f"reverted {count}")
