import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ChevronDown, ChevronRight, ExternalLink } from "lucide-react";

import { api } from "@/api/client";
import type { IncidentRun, RemediationAgent, ScanRun, ScanRunList } from "@/api/types";
import { ModelTokensList } from "@/components/ModelTokens";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent } from "@/components/ui/dialog";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { formatTimestamp } from "../dashboard/columns";
import { IncidentDetail } from "../dashboard/IncidentDetail";
import { ScanStatusBadge, scanTriggerLabels } from "./labels";

const PAGE_SIZE = 20;

function duration(run: ScanRun): string {
  if (!run.finished_at) return "";
  const seconds = Math.round((Date.parse(run.finished_at) - Date.parse(run.started_at)) / 1000);
  return seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
}

/** What started the run: a SHA is shortened, a scheduled time is shown as is. */
function triggerRef(run: ScanRun): string {
  return /^[0-9a-f]{40}$/.test(run.trigger_ref) ? run.trigger_ref.slice(0, 7) : run.trigger_ref;
}

function Repos({ run, onOpenIncident }: { run: ScanRun; onOpenIncident: (id: number) => void }) {
  if (run.repos.length === 0) {
    return <p className="text-sm text-muted-foreground">No repos in this scan.</p>;
  }
  return (
    <div className="rounded-md border">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Repo</TableHead>
            <TableHead>Status</TableHead>
            <TableHead>Findings</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {run.repos.map((repo) => (
            <TableRow key={repo.project_id}>
              <TableCell className="font-medium">{repo.project_name}</TableCell>
              <TableCell>
                <ScanStatusBadge status={repo.status} />
                {repo.error && (
                  <p
                    className={`mt-1 text-xs ${repo.status === "skipped" ? "text-muted-foreground" : "text-destructive"}`}
                  >
                    {repo.error}
                  </p>
                )}
              </TableCell>
              <TableCell>
                {repo.findings.length === 0 ? (
                  <span className="text-muted-foreground">{repo.finding_count || "—"}</span>
                ) : (
                  <ul className="space-y-1">
                    {repo.findings.map((finding) => (
                      <li key={finding.incident_run_id} className="flex flex-wrap items-center gap-2">
                        <Button
                          variant="outline"
                          size="sm"
                          onClick={() => onOpenIncident(finding.incident_run_id)}
                        >
                          Incident #{finding.incident_run_id}
                        </Button>
                        {finding.pr_url ? (
                          <a
                            href={finding.pr_url}
                            target="_blank"
                            rel="noreferrer"
                            className="inline-flex items-center gap-1 text-xs text-primary hover:underline"
                          >
                            PR #{finding.pr_url.split("/").pop()}
                            <ExternalLink className="h-3 w-3" />
                          </a>
                        ) : (
                          finding.mode_note && (
                            <span className="text-xs text-muted-foreground">{finding.mode_note}</span>
                          )
                        )}
                      </li>
                    ))}
                  </ul>
                )}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}

function IncidentDialog({ id, onClose }: { id: number | null; onClose: () => void }) {
  const incident = useQuery({
    queryKey: ["incident-run", id],
    queryFn: () => api.get<IncidentRun>(`/sre/incident-runs/${id}`),
    enabled: id != null,
    // Its fix may still be running.
    refetchInterval: 15000,
  });
  return (
    <Dialog open={id != null} onOpenChange={(open) => !open && onClose()}>
      <DialogContent aria-describedby={undefined} className="w-[calc(100%-2rem)] max-w-5xl p-0">
        {incident.data && <IncidentDetail run={incident.data} onClose={onClose} />}
        {incident.isLoading && <p className="p-6 text-sm text-muted-foreground">Loading incident...</p>}
        {incident.isError && <p className="p-6 text-sm text-destructive">Could not load incident.</p>}
      </DialogContent>
    </Dialog>
  );
}

export function ScanRuns({ agent }: { agent: RemediationAgent }) {
  const [expanded, setExpanded] = useState<number | null>(null);
  const [incidentId, setIncidentId] = useState<number | null>(null);

  const runs = useQuery({
    queryKey: ["scan-runs", agent.id],
    queryFn: () => api.get<ScanRunList>(`/sre/agents/${agent.id}/scan-runs?page_size=${PAGE_SIZE}`),
    // Poll while a scan is running; its repos finish one by one.
    refetchInterval: (query) =>
      query.state.data?.scan_runs.some((r) => r.status === "running") ? 5000 : false,
  });

  if (runs.isLoading) return <p className="text-sm text-muted-foreground">Loading scan runs...</p>;
  if (runs.isError) return <p className="text-sm text-destructive">Could not load scan runs.</p>;
  const list = runs.data?.scan_runs ?? [];
  if (list.length === 0) {
    return <p className="text-sm text-muted-foreground">This agent hasn't run yet.</p>;
  }
  // The latest run is open until the user picks another.
  const open = expanded ?? list[0].id;

  return (
    <div className="space-y-2">
      <ul className="space-y-2">
        {list.map((run) => {
          const isOpen = run.id === open;
          return (
            <li key={run.id} className="rounded-md border">
              <button
                type="button"
                className="flex w-full flex-wrap items-center gap-x-3 gap-y-1 p-3 text-left text-sm"
                aria-expanded={isOpen}
                onClick={() => setExpanded(isOpen ? -1 : run.id)}
              >
                {isOpen ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
                <span className="font-mono text-xs">{formatTimestamp(run.started_at)} UTC</span>
                <ScanStatusBadge status={run.status} />
                <span>{scanTriggerLabels[run.trigger]}</span>
                {run.trigger_ref && (
                  <span className="font-mono text-xs text-muted-foreground">{triggerRef(run)}</span>
                )}
                <span className="ml-auto flex items-center gap-3 text-xs text-muted-foreground">
                  <span>
                    {run.finding_count} finding{run.finding_count === 1 ? "" : "s"}
                  </span>
                  <span>{run.usage.total_tokens.toLocaleString()} tokens</span>
                  {duration(run) && <span>{duration(run)}</span>}
                </span>
              </button>
              {isOpen && (
                <div className="space-y-2 border-t p-3">
                  {run.error_message && (
                    <p className="whitespace-pre-wrap text-sm text-destructive">{run.error_message}</p>
                  )}
                  <Repos run={run} onOpenIncident={setIncidentId} />
                  {run.usage.by_model.length > 0 && (
                    <div className="max-w-sm space-y-1">
                      <p className="text-xs font-medium text-muted-foreground">Tokens by model</p>
                      <ModelTokensList models={run.usage.by_model} />
                    </div>
                  )}
                </div>
              )}
            </li>
          );
        })}
      </ul>
      {runs.data && runs.data.total > list.length && (
        <p className="text-xs text-muted-foreground">
          Showing the latest {list.length} of {runs.data.total} scan runs.
        </p>
      )}
      <IncidentDialog id={incidentId} onClose={() => setIncidentId(null)} />
    </div>
  );
}
