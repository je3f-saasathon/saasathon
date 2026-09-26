import type { Column, ColumnDef } from "@tanstack/react-table";
import { ArrowUpDown, ExternalLink } from "lucide-react";

import type { IncidentRun, IncidentStatus } from "@/api/types";
import { Badge } from "@/components/ui/badge";
import { ModelTokensList } from "@/components/ModelTokens";
import { Button } from "@/components/ui/button";

type BadgeVariant = React.ComponentProps<typeof Badge>["variant"];

export const statusVariant: Record<IncidentStatus, BadgeVariant> = {
  running: "secondary",
  awaiting_approval: "default",
  succeeded: "outline",
  advisory_complete: "outline",
  new_playbook_created: "secondary",
  no_anomaly: "outline",
  failed: "destructive",
  // A person closed the PR unmerged: a decision, not an error, so not red.
  rejected: "outline",
};

// shadcn's Badge has no "success"/"warning" variants; tint the outline/default ones.
export const statusClassName: Partial<Record<IncidentStatus, string>> = {
  succeeded: "border-emerald-500 text-emerald-700 dark:text-emerald-300",
  awaiting_approval: "bg-amber-500 text-white hover:bg-amber-500/80",
  rejected: "border-muted-foreground/50 text-muted-foreground",
};

export function StatusBadge({ status }: { status: string }) {
  const s = status as IncidentStatus;
  return (
    <Badge variant={statusVariant[s] ?? "outline"} className={`whitespace-nowrap ${statusClassName[s] ?? ""}`}>
      {status.replace(/_/g, " ")}
    </Badge>
  );
}

/**
 * The one-line description of an incident: the classifier's summary, else a scan
 * finding's title, else the trace id.
 */
export function incidentSummary(run: IncidentRun): string {
  const summary = run.classification?.summary;
  if (typeof summary === "string" && summary) return summary;
  const title = run.telemetry?.title;
  if (run.source === "scan" && typeof title === "string" && title) return title;
  return `Trace ${run.trace_id}`;
}

/** Marks incidents a remediation agent found, rather than an alert. */
export function SourceBadge({ run }: { run: IncidentRun }) {
  if (run.source !== "scan") return null;
  return (
    <Badge variant="secondary" className="whitespace-nowrap" title="Found by a remediation agent">
      scan
    </Badge>
  );
}

export function PrLink({ run }: { run: IncidentRun }) {
  if (!run.pr_url) return <span className="text-muted-foreground">—</span>;
  const draft = run.playbook_run_status === "pending_approval";
  return (
    <a
      href={run.pr_url}
      target="_blank"
      rel="noreferrer"
      onClick={(event) => event.stopPropagation()}
      className="inline-flex items-center gap-1 whitespace-nowrap text-sm font-medium underline-offset-4 hover:underline"
    >
      {draft ? "Draft PR" : "PR"} #{run.pr_url.split("/").pop()}
      <ExternalLink className="h-3 w-3" />
    </a>
  );
}

/** Whose key paid for a run's LLM calls: ours (company default), the user's, or both. */
export function billedLabel(run: IncidentRun): string | null {
  const { calls, platform_tokens, total_tokens } = run.usage;
  if (calls === 0) return null;
  if (platform_tokens === 0) return "your key";
  return platform_tokens >= total_tokens ? "our key" : "our + your key";
}

/** Under the token count: whether tests were written (frozen per run) and whose key paid. */
export function TestsNote({ run }: { run: IncidentRun }) {
  const parts = [
    run.generate_tests == null ? null : `tests ${run.generate_tests ? "on" : "off"}`,
    billedLabel(run),
  ].filter(Boolean);
  if (!parts.length) return null;
  return <div className="text-[11px] text-muted-foreground">{parts.join(" · ")}</div>;
}

function SortableHeader({ column, title }: { column: Column<IncidentRun>; title: string }) {
  return (
    <Button
      variant="ghost"
      className="-ml-3"
      onClick={() => column.toggleSorting(column.getIsSorted() === "asc")}
    >
      {title}
      <ArrowUpDown />
    </Button>
  );
}

export function formatTimestamp(iso: string) {
  return iso.replace("T", " ").slice(0, 19);
}

export const columns: ColumnDef<IncidentRun>[] = [
  {
    accessorKey: "created_at",
    header: ({ column }) => <SortableHeader column={column} title="Time (UTC)" />,
    cell: ({ row }) => (
      <span className="whitespace-nowrap font-mono text-xs">
        {formatTimestamp(row.getValue("created_at"))}
      </span>
    ),
  },
  {
    accessorKey: "project_name",
    id: "project",
    header: ({ column }) => <SortableHeader column={column} title="Project" />,
    cell: ({ row }) => <span className="whitespace-nowrap">{row.getValue("project")}</span>,
  },
  {
    id: "incident",
    accessorFn: incidentSummary,
    header: "Incident",
    cell: ({ row }) => (
      <div className="flex min-w-[10rem] max-w-[16rem] items-center gap-2">
        <SourceBadge run={row.original} />
        <span className="truncate" title={row.getValue("incident")}>
          {row.getValue("incident")}
        </span>
      </div>
    ),
  },
  {
    accessorKey: "status",
    header: ({ column }) => <SortableHeader column={column} title="Status" />,
    cell: ({ row }) => <StatusBadge status={row.getValue("status")} />,
  },
  {
    id: "playbook",
    accessorFn: (run) => run.playbook?.title ?? "",
    header: "Playbook",
    cell: ({ row }) => {
      const title = row.getValue<string>("playbook");
      return title ? (
        <div className="max-w-[11rem] truncate" title={title}>
          {title}
        </div>
      ) : (
        <span className="text-muted-foreground">—</span>
      );
    },
  },
  {
    id: "pr",
    header: "PR",
    cell: ({ row }) => <PrLink run={row.original} />,
  },
  {
    id: "model",
    accessorFn: (run) => run.usage.models.join(", "),
    header: "Model",
    // Each model with its own tokens: models are priced differently.
    cell: ({ row }) =>
      row.original.usage.by_model.length ? (
        <ModelTokensList models={row.original.usage.by_model} />
      ) : (
        <span className="text-muted-foreground">n/a</span>
      ),
  },
  {
    id: "tokens",
    accessorFn: (run) => run.usage.total_tokens,
    header: ({ column }) => <SortableHeader column={column} title="Tokens" />,
    cell: ({ row }) =>
      row.original.usage.calls === 0 ? (
        <span className="text-muted-foreground">n/a</span>
      ) : (
        <div className="whitespace-nowrap">
          <div className="font-mono text-xs">{row.getValue<number>("tokens").toLocaleString()}</div>
          <TestsNote run={row.original} />
        </div>
      ),
  },
];
