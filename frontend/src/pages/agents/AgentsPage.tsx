import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Pause, Pencil, Play, Plus, Power, Trash2 } from "lucide-react";
import { useSearchParams } from "react-router-dom";

import { api, ApiError } from "@/api/client";
import type { Organization, RemediationAgent, ScanRun } from "@/api/types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Select } from "@/components/ui/select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { cn } from "@/lib/utils";
import { ErrorText, Field } from "../settings/form";
import { useOrganizations } from "../settings/ProjectsTab";
import { AgentForm } from "./AgentForm";
import { describeTrigger, kindLabels, modeLabels } from "./labels";
import { ScanRuns } from "./ScanRuns";

const isOrgAdmin = (org?: Organization) => org?.role === "owner" || org?.role === "admin";

function DisabledNotice() {
  return (
    <div className="space-y-2 rounded-md border border-amber-500/60 bg-amber-50 p-4 text-sm dark:bg-amber-500/10">
      <p className="font-medium">Remediation agents are turned off on this server.</p>
      <p className="text-muted-foreground">
        Set <code className="rounded bg-muted px-1 font-mono text-xs">SRE_REMEDIATION_AGENTS_ENABLED=true</code>{" "}
        in <code className="rounded bg-muted px-1 font-mono text-xs">backend/.env</code>, then recreate the
        backend and worker containers. The Quiet errors kind and runbook ordering also need{" "}
        <code className="rounded bg-muted px-1 font-mono text-xs">SRE_SERVICE_MESH_ENABLED=true</code>. For
        the merge and push triggers, subscribe the GitHub App to the Pull request and Push events.
      </p>
    </div>
  );
}

function tokensNote(agent: RemediationAgent): string {
  const used = agent.tokens_this_month.toLocaleString();
  return agent.monthly_token_budget
    ? `${used} / ${agent.monthly_token_budget.toLocaleString()}`
    : used;
}

export function AgentsPage() {
  const queryClient = useQueryClient();
  const [params, setParams] = useSearchParams();
  const [editing, setEditing] = useState<number | "new" | null>(null);

  // An incident links here with just ?agent=; look up its org.
  const agentParam = params.get("agent") ? Number(params.get("agent")) : null;
  const orgParam = params.get("org") ? Number(params.get("org")) : null;
  const linkedAgent = useQuery({
    queryKey: ["agent", agentParam],
    queryFn: () => api.get<RemediationAgent>(`/sre/agents/${agentParam}`),
    enabled: agentParam != null && orgParam == null,
  });

  const orgs = useOrganizations();
  const orgList = orgs.data ?? [];
  const wantedOrg = orgParam ?? linkedAgent.data?.organization_id;
  const org = orgList.find((o) => o.id === wantedOrg) ?? orgList[0];
  const admin = isOrgAdmin(org);

  const agents = useQuery({
    queryKey: ["agents", org?.id],
    queryFn: () => api.get<RemediationAgent[]>(`/sre/organizations/${org!.id}/agents`),
    enabled: org != null,
    // Token counts and last runs move while scans run.
    refetchInterval: 30000,
  });
  const disabled = agents.error instanceof ApiError && agents.error.status === 404;
  const list = agents.data ?? [];
  const selected = list.find((a) => a.id === agentParam) ?? list[0];

  function select(agent: RemediationAgent) {
    setParams({ org: String(agent.organization_id), agent: String(agent.id) });
  }

  const invalidate = (agentId: number) => {
    queryClient.invalidateQueries({ queryKey: ["agents", org?.id] });
    queryClient.invalidateQueries({ queryKey: ["scan-runs", agentId] });
  };
  const run = useMutation({
    mutationFn: (agent: RemediationAgent) => api.post<ScanRun>(`/sre/agents/${agent.id}/run`),
    onSuccess: (_, agent) => {
      select(agent);
      invalidate(agent.id);
    },
  });
  const setEnabled = useMutation({
    mutationFn: (agent: RemediationAgent) =>
      api.patch<RemediationAgent>(`/sre/agents/${agent.id}`, { enabled: !agent.enabled }),
    onSuccess: (agent) => invalidate(agent.id),
  });
  const remove = useMutation({
    mutationFn: (agent: RemediationAgent) => api.delete(`/sre/agents/${agent.id}`),
    onSuccess: (_, agent) => {
      if (agent.id === agentParam) setParams({ org: String(agent.organization_id) });
      invalidate(agent.id);
    },
  });
  const actionError = run.error ?? setEnabled.error ?? remove.error;
  const editingAgent = typeof editing === "number" ? list.find((a) => a.id === editing) : undefined;

  return (
    <div className="mx-auto max-w-6xl space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Remediation agents</h1>
        <p className="text-sm text-muted-foreground">
          Agents that scan your organization's repos for bugs before they raise an alert. Each
          finding becomes an incident and goes through the same checks, playbooks and approvals as
          an Uptrace alert.
        </p>
      </div>

      {orgList.length > 1 && (
        <div className="max-w-xs">
          <Field label="Organization">
            <Select
              value={org?.id ?? ""}
              onChange={(e) => {
                setEditing(null);
                setParams({ org: e.target.value });
              }}
            >
              {orgList.map((o) => (
                <option key={o.id} value={o.id}>
                  {o.name}
                </option>
              ))}
            </Select>
          </Field>
        </div>
      )}

      <Card>
        <CardHeader className="flex flex-row items-start justify-between gap-4 space-y-0">
          <div className="space-y-1">
            <CardTitle>Agents</CardTitle>
            <CardDescription>
              {admin
                ? "Each agent scans some or all of the organization's repos when its trigger fires, or when you run it."
                : "Only organization admins can create, change or run agents."}
            </CardDescription>
          </div>
          {admin && !disabled && editing == null && (
            <Button onClick={() => setEditing("new")}>
              <Plus /> New agent
            </Button>
          )}
        </CardHeader>
        <CardContent className="space-y-4">
          {(orgs.isLoading || agents.isLoading) && (
            <p className="text-sm text-muted-foreground">Loading...</p>
          )}
          {orgs.data && orgList.length === 0 && (
            <p className="text-sm text-muted-foreground">You aren't in any organization yet.</p>
          )}
          {disabled && <DisabledNotice />}
          {agents.isError && !disabled && (
            <p className="text-sm text-destructive">Could not load agents.</p>
          )}
          {org && editing === "new" && (
            <AgentForm
              org={org}
              onDone={(saved) => {
                setEditing(null);
                if (saved) select(saved);
              }}
            />
          )}
          {org && editingAgent && (
            <AgentForm
              key={editingAgent.id}
              org={org}
              agent={editingAgent}
              onDone={() => setEditing(null)}
            />
          )}
          {agents.data && list.length === 0 && editing == null && (
            <p className="text-sm text-muted-foreground">
              No agents yet.
              {admin && " Start with a Playbook sweep on merge: it scans each merged diff for known kinds of bug."}
            </p>
          )}
          {list.length > 0 && (
            <div className="rounded-md border">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Name</TableHead>
                    <TableHead>Looks for</TableHead>
                    <TableHead>Runs</TableHead>
                    <TableHead>Repos</TableHead>
                    <TableHead>Fixes</TableHead>
                    <TableHead className="text-right">Tokens this month</TableHead>
                    {admin && <TableHead />}
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {list.map((agent) => (
                    <TableRow
                      key={agent.id}
                      data-state={agent.id === selected?.id ? "selected" : undefined}
                      className="cursor-pointer"
                      onClick={() => select(agent)}
                    >
                      <TableCell className="font-medium">
                        <div className="flex items-center gap-2">
                          {agent.name}
                          {!agent.enabled && <Badge variant="outline">paused</Badge>}
                        </div>
                      </TableCell>
                      <TableCell>{kindLabels[agent.kind]}</TableCell>
                      <TableCell className="whitespace-nowrap">
                        <span className={cn(agent.trigger !== "on_merge" && "font-mono text-xs")}>
                          {describeTrigger(agent)}
                        </span>
                      </TableCell>
                      <TableCell>
                        {agent.project_ids.length === 0 ? "All" : agent.project_ids.length}
                      </TableCell>
                      <TableCell className="whitespace-nowrap">
                        {modeLabels[agent.execution_mode] ?? agent.execution_mode}
                      </TableCell>
                      <TableCell className="text-right font-mono text-xs">{tokensNote(agent)}</TableCell>
                      {admin && (
                        <TableCell className="text-right" onClick={(e) => e.stopPropagation()}>
                          <div className="flex justify-end gap-1">
                            <Button
                              variant="ghost"
                              size="icon"
                              aria-label={`Run ${agent.name} now`}
                              title="Run now"
                              disabled={!agent.enabled || run.isPending}
                              onClick={() => run.mutate(agent)}
                            >
                              <Play />
                            </Button>
                            <Button
                              variant="ghost"
                              size="icon"
                              aria-label={`${agent.enabled ? "Pause" : "Resume"} ${agent.name}`}
                              title={agent.enabled ? "Pause" : "Resume"}
                              disabled={setEnabled.isPending}
                              onClick={() => setEnabled.mutate(agent)}
                            >
                              {agent.enabled ? <Pause /> : <Power />}
                            </Button>
                            <Button
                              variant="ghost"
                              size="icon"
                              aria-label={`Edit ${agent.name}`}
                              title="Edit"
                              onClick={() => setEditing(agent.id)}
                            >
                              <Pencil />
                            </Button>
                            <Button
                              variant="ghost"
                              size="icon"
                              aria-label={`Delete ${agent.name}`}
                              title="Delete"
                              onClick={() => {
                                if (
                                  window.confirm(
                                    `Delete "${agent.name}"? Its scan history goes too; the incidents it found stay.`,
                                  )
                                ) {
                                  remove.mutate(agent);
                                }
                              }}
                            >
                              <Trash2 />
                            </Button>
                          </div>
                        </TableCell>
                      )}
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
          <ErrorText error={actionError} />
        </CardContent>
      </Card>

      {selected && (
        <Card>
          <CardHeader>
            <CardTitle>Scan runs · {selected.name}</CardTitle>
            <CardDescription>
              Each run scans its repos two at a time. Open a finding to follow its incident.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <ScanRuns agent={selected} />
          </CardContent>
        </Card>
      )}
    </div>
  );
}
