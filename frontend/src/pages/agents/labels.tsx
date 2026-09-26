import type { AgentKind, AgentTrigger, RemediationAgent, ScanTrigger } from "@/api/types";
import { Badge } from "@/components/ui/badge";

export const kindLabels: Record<AgentKind, string> = {
  playbook_sweep: "Playbook sweep",
  runbook_variant: "Runbook variants",
  find_quiet: "Quiet errors",
};

export const kindDescriptions: Record<AgentKind, string> = {
  playbook_sweep: "Bugs matching the chosen playbooks. Findings get a diagnosis only.",
  runbook_variant:
    "New occurrences of bugs a runbook already fixed, in this repo or others. Can open draft PRs.",
  find_quiet:
    "Uptrace errors and slow or failing calls between services that never raised an alert. Needs the service mesh.",
};

export const triggerLabels: Record<AgentTrigger, string> = {
  on_merge: "On merge",
  branch_watch: "Branch push",
  schedule: "Schedule",
};

export const triggerDescriptions: Record<AgentTrigger, string> = {
  on_merge: "When a PR merges into a repo's default branch. Scans only the merged diff.",
  branch_watch: "When a push lands on a matching branch. Scans only the pushed diff.",
  schedule: "On a cron schedule (UTC). Scans the whole repo.",
};

export const scanTriggerLabels: Record<ScanTrigger, string> = {
  manual: "Merge",
  on_merge: "Merge",
  branch_watch: "Push",
  schedule: "Schedule",
};

export const modeLabels: Record<string, string> = {
  advisory_only: "Diagnosis only",
  draft_only: "Draft PR",
};

/** The agent's trigger with its detail, e.g. "Schedule · 0 3 * * *". */
export function describeTrigger(agent: RemediationAgent): string {
  const label = triggerLabels[agent.trigger];
  if (agent.trigger === "schedule") return `${label} · ${agent.schedule_cron}`;
  if (agent.trigger === "branch_watch") return `${label} · ${agent.branch_pattern}`;
  return label;
}

const scanStatusClassName: Record<string, string> = {
  succeeded: "border-emerald-500 text-emerald-700 dark:text-emerald-300",
  partial: "border-amber-500 text-amber-700 dark:text-amber-300",
  skipped: "border-muted-foreground/50 text-muted-foreground",
  pending: "border-muted-foreground/50 text-muted-foreground",
};

/** Status of a scan run or of one repo in it. */
export function ScanStatusBadge({ status }: { status: string }) {
  const variant = status === "failed" ? "destructive" : status === "running" ? "secondary" : "outline";
  return (
    <Badge variant={variant} className={`whitespace-nowrap ${scanStatusClassName[status] ?? ""}`}>
      {status === "partial" ? "some repos failed" : status}
    </Badge>
  );
}
