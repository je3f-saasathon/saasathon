import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ChevronDown, ChevronRight, ExternalLink, X } from "lucide-react";

import { api } from "@/api/client";
import type { IncidentRun, Playbook, PlaybookRun } from "@/api/types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { StatusBadge, formatTimestamp, incidentSummary } from "./columns";

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="space-y-2">
      <h3 className="text-sm font-semibold">{title}</h3>
      {children}
    </section>
  );
}

function str(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function Diagnosis({ run, playbookRun }: { run: IncidentRun; playbookRun?: PlaybookRun }) {
  if (run.diagnosis_report) {
    return <p className="whitespace-pre-wrap text-sm">{run.diagnosis_report}</p>;
  }
  const c = run.classification ?? {};
  const anomaly = (c.anomaly ?? {}) as Record<string, unknown>;
  const suspected = Array.isArray(c.suspected_files) ? (c.suspected_files as string[]) : [];
  const attempts = playbookRun?.attempts ?? [];
  const lastSummary = [...attempts].reverse().find((a) => a.summary)?.summary;

  return (
    <dl className="grid gap-x-4 gap-y-2 text-sm sm:grid-cols-[10rem_1fr]">
      {str(c.category) && (
        <>
          <dt className="text-muted-foreground">Category</dt>
          <dd>
            {str(c.category)} · severity {str(c.severity) || "unknown"}
          </dd>
        </>
      )}
      {str(c.summary) && (
        <>
          <dt className="text-muted-foreground">Summary</dt>
          <dd className="whitespace-pre-wrap">{str(c.summary)}</dd>
        </>
      )}
      {suspected.length > 0 && (
        <>
          <dt className="text-muted-foreground">Suspected files</dt>
          <dd className="font-mono text-xs">{suspected.join(", ")}</dd>
        </>
      )}
      {str(anomaly.reasoning) && (
        <>
          <dt className="text-muted-foreground">Anomaly check</dt>
          <dd className="whitespace-pre-wrap">{str(anomaly.reasoning)}</dd>
        </>
      )}
      {lastSummary && (
        <>
          <dt className="text-muted-foreground">Agent's fix</dt>
          <dd className="whitespace-pre-wrap">{lastSummary}</dd>
        </>
      )}
      {!run.classification && !lastSummary && (
        <dd className="text-muted-foreground">No diagnosis yet.</dd>
      )}
    </dl>
  );
}

function PlaybookSteps({ playbook }: { playbook: Playbook }) {
  return (
    <div className="space-y-2 text-sm">
      {playbook.description && <p className="text-muted-foreground">{playbook.description}</p>}
      <ol className="list-decimal space-y-1 pl-5">
        {playbook.steps.map((step, i) => (
          <li key={i}>
            {step.type === "run_command" ? (
              <code className="rounded bg-muted px-1 font-mono text-xs">{str(step.command)}</code>
            ) : (
              <>
                <span className="font-mono text-xs">{str(step.path)}</span>
                {str(step.instructions) && <span> — {str(step.instructions)}</span>}
              </>
            )}
          </li>
        ))}
      </ol>
    </div>
  );
}

function Attempts({ playbookRun }: { playbookRun: PlaybookRun }) {
  const [open, setOpen] = useState<number | null>(null);
  return (
    <ul className="space-y-2 text-sm">
      {playbookRun.attempts.map((attempt) => (
        <li key={attempt.attempt_number} className="rounded-md border p-3">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-medium">Attempt {attempt.attempt_number}</span>
            <Badge variant={attempt.outcome === "failed" ? "destructive" : "outline"}>
              {attempt.outcome}
            </Badge>
            {attempt.branch_name && (
              <span className="font-mono text-xs text-muted-foreground">{attempt.branch_name}</span>
            )}
          </div>
          {attempt.summary && <p className="mt-2 whitespace-pre-wrap">{attempt.summary}</p>}
          {attempt.error_output && (
            <div className="mt-2">
              <Button
                variant="ghost"
                size="sm"
                className="-ml-3"
                onClick={() =>
                  setOpen(open === attempt.attempt_number ? null : attempt.attempt_number)
                }
              >
                {open === attempt.attempt_number ? <ChevronDown /> : <ChevronRight />}
                Error output
              </Button>
              {open === attempt.attempt_number && (
                <pre className="max-h-64 overflow-auto whitespace-pre-wrap rounded bg-muted p-2 font-mono text-xs">
                  {attempt.error_output}
                </pre>
              )}
            </div>
          )}
        </li>
      ))}
    </ul>
  );
}

function UsageTable({ run }: { run: IncidentRun }) {
  const { usage } = run;
  if (usage.calls === 0) {
    return <p className="text-sm text-muted-foreground">No LLM usage recorded for this incident.</p>;
  }
  return (
    <div className="rounded-md border">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Step</TableHead>
            <TableHead>Model</TableHead>
            <TableHead className="text-right">Calls</TableHead>
            <TableHead className="text-right">Input</TableHead>
            <TableHead className="text-right">Output</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {usage.by_step.map((s) => (
            <TableRow key={`${s.step}-${s.model}`}>
              <TableCell>{s.step.replace(/_/g, " ")}</TableCell>
              <TableCell className="font-mono text-xs">{s.model || "unknown"}</TableCell>
              <TableCell className="text-right">{s.calls}</TableCell>
              <TableCell className="text-right font-mono text-xs">
                {s.input_tokens.toLocaleString()}
              </TableCell>
              <TableCell className="text-right font-mono text-xs">
                {s.output_tokens.toLocaleString()}
              </TableCell>
            </TableRow>
          ))}
          <TableRow className="font-medium">
            <TableCell colSpan={2}>Total ({usage.total_tokens.toLocaleString()} tokens)</TableCell>
            <TableCell className="text-right">{usage.calls}</TableCell>
            <TableCell className="text-right font-mono text-xs">
              {usage.input_tokens.toLocaleString()}
            </TableCell>
            <TableCell className="text-right font-mono text-xs">
              {usage.output_tokens.toLocaleString()}
            </TableCell>
          </TableRow>
        </TableBody>
      </Table>
    </div>
  );
}

export function IncidentDetail({ run, onClose }: { run: IncidentRun; onClose: () => void }) {
  const [showDiagnosis, setShowDiagnosis] = useState(false);
  const playbookId = run.playbook?.id;
  const playbookRunId = run.playbook_run_id;

  const playbook = useQuery({
    queryKey: ["playbook", playbookId],
    queryFn: () => api.get<Playbook>(`/sre/playbooks/${playbookId}`),
    enabled: playbookId != null,
  });
  const playbookRun = useQuery({
    queryKey: ["playbook-run", playbookRunId],
    queryFn: () => api.get<PlaybookRun>(`/sre/playbook-runs/${playbookRunId}`),
    enabled: playbookRunId != null,
  });

  return (
    <Card>
      <CardHeader className="flex flex-row items-start justify-between gap-4 space-y-0">
        <div className="space-y-1">
          <CardTitle className="text-base">{incidentSummary(run)}</CardTitle>
          <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            <StatusBadge status={run.status} />
            <span>{run.project_name}</span>
            <span className="font-mono">trace {run.trace_id}</span>
            <span className="font-mono">{formatTimestamp(run.created_at)} UTC</span>
            {run.execution_mode && <span>{run.execution_mode.replace(/_/g, " ")}</span>}
            {run.generate_tests != null && (
              <Badge variant="outline">tests {run.generate_tests ? "on" : "off"}</Badge>
            )}
          </div>
        </div>
        <div className="flex items-center gap-2">
          {run.pr_url && (
            <Button asChild variant="outline" size="sm">
              <a href={run.pr_url} target="_blank" rel="noreferrer">
                {run.playbook_run_status === "pending_approval" ? "Open draft PR" : "Open PR"}
                <ExternalLink />
              </a>
            </Button>
          )}
          <Button variant="ghost" size="icon" onClick={onClose} aria-label="Close details">
            <X />
          </Button>
        </div>
      </CardHeader>
      <CardContent className="space-y-6">
        {run.error_message && (
          <p className="whitespace-pre-wrap text-sm text-destructive">{run.error_message}</p>
        )}

        <Section title="Diagnosis">
          <Button variant="outline" size="sm" onClick={() => setShowDiagnosis((v) => !v)}>
            {showDiagnosis ? <ChevronDown /> : <ChevronRight />}
            {showDiagnosis ? "Hide diagnosis" : "Show diagnosis"}
          </Button>
          {showDiagnosis && <Diagnosis run={run} playbookRun={playbookRun.data} />}
        </Section>

        {run.playbook && (
          <Section title="Playbook used">
            <div className="flex flex-wrap items-center gap-2 text-sm">
              <span className="font-medium">{run.playbook.title}</span>
              <Badge variant="outline">{run.playbook.status}</Badge>
              <Badge variant="secondary">
                {run.playbook.source === "created" ? "written for this incident" : "reused"}
              </Badge>
            </div>
            {playbook.isLoading && <p className="text-sm text-muted-foreground">Loading steps...</p>}
            {playbook.isError && <p className="text-sm text-destructive">Could not load playbook.</p>}
            {playbook.data && <PlaybookSteps playbook={playbook.data} />}
          </Section>
        )}

        {playbookRunId != null && (
          <Section title="Fix attempts">
            {playbookRun.isLoading && <p className="text-sm text-muted-foreground">Loading...</p>}
            {playbookRun.isError && (
              <p className="text-sm text-destructive">Could not load attempts.</p>
            )}
            {playbookRun.data &&
              (playbookRun.data.attempts.length ? (
                <Attempts playbookRun={playbookRun.data} />
              ) : (
                <p className="text-sm text-muted-foreground">No attempts yet.</p>
              ))}
          </Section>
        )}

        <Section title="LLM usage">
          <UsageTable run={run} />
        </Section>
      </CardContent>
    </Card>
  );
}
