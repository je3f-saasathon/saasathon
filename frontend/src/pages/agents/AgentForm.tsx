import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "@/api/client";
import type {
  AgentKind,
  AgentRequest,
  AgentTrigger,
  Organization,
  PlaybookList,
  Project,
  RemediationAgent,
} from "@/api/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { ErrorText, Field } from "../settings/form";
import { kindDescriptions, kindLabels, triggerDescriptions, triggerLabels } from "./labels";

// "" leaves the choice to the server, which defaults by kind.
type FormMode = "" | "advisory_only" | "draft_only";

type FormState = Omit<AgentRequest, "execution_mode"> & { execution_mode: FormMode };

const defaultModeLabel: Record<AgentKind, string> = {
  playbook_sweep: "Default (diagnosis only)",
  runbook_variant: "Default (draft PR)",
  find_quiet: "Default (draft PR)",
};

function initialForm(agent?: RemediationAgent): FormState {
  if (!agent) {
    return {
      name: "",
      kind: "playbook_sweep",
      trigger: "on_merge",
      schedule_cron: "",
      branch_pattern: "",
      project_ids: [],
      playbook_ids: [],
      execution_mode: "",
      max_findings_per_repo: 3,
      monthly_token_budget: 0,
      enabled: true,
    };
  }
  return {
    name: agent.name,
    kind: agent.kind,
    trigger: agent.trigger,
    schedule_cron: agent.schedule_cron,
    branch_pattern: agent.branch_pattern,
    project_ids: agent.project_ids,
    playbook_ids: agent.playbook_ids,
    execution_mode: agent.execution_mode === "draft_only" ? "draft_only" : "advisory_only",
    max_findings_per_repo: agent.max_findings_per_repo,
    monthly_token_budget: agent.monthly_token_budget,
    enabled: agent.enabled,
  };
}

function toggle(ids: number[], id: number, on: boolean): number[] {
  return on ? [...ids, id] : ids.filter((x) => x !== id);
}

/**
 * Picks some ids from a list, or none for "all of them". `allLabel` describes what
 * an empty selection means.
 */
function IdPicker({
  legend,
  allLabel,
  items,
  selected,
  onChange,
}: {
  legend: string;
  allLabel: string;
  items: { id: number; label: string; note?: string }[];
  selected: number[];
  onChange: (ids: number[]) => void;
}) {
  const all = selected.length === 0;
  // Remember that "pick some" was chosen even before anything is ticked.
  const [picking, setPicking] = useState(!all);
  return (
    <fieldset className="space-y-2 text-sm">
      <legend className="font-medium">{legend}</legend>
      <label className="flex items-center gap-2">
        <input
          type="checkbox"
          checked={all && !picking}
          onChange={(e) => {
            setPicking(!e.target.checked);
            if (e.target.checked) onChange([]);
          }}
        />
        {allLabel}
      </label>
      {(picking || !all) && (
        <div className="max-h-48 space-y-1 overflow-y-auto rounded-md border p-2">
          {items.length === 0 && <p className="text-xs text-muted-foreground">Nothing to choose from.</p>}
          {items.map((item) => (
            <label key={item.id} className="flex items-center gap-2">
              <input
                type="checkbox"
                checked={selected.includes(item.id)}
                onChange={(e) => onChange(toggle(selected, item.id, e.target.checked))}
              />
              <span>{item.label}</span>
              {item.note && <span className="text-xs text-muted-foreground">{item.note}</span>}
            </label>
          ))}
        </div>
      )}
    </fieldset>
  );
}

export function AgentForm({
  org,
  agent,
  onDone,
}: {
  org: Organization;
  agent?: RemediationAgent;
  onDone: (agent?: RemediationAgent) => void;
}) {
  const queryClient = useQueryClient();
  const [form, setForm] = useState<FormState>(() => initialForm(agent));

  const projects = useQuery({
    queryKey: ["projects"],
    queryFn: () => api.get<Project[]>("/sre/projects"),
  });
  const orgProjects = (projects.data ?? []).filter((p) => p.organization_id === org.id);

  // There's no org-wide playbook list: any org project's list has the built-ins and the
  // org's generic playbooks, which are the ones an agent may use.
  const sampleProject = orgProjects[0];
  const playbooks = useQuery({
    queryKey: ["playbooks", sampleProject?.id],
    queryFn: () => api.get<PlaybookList>(`/sre/projects/${sampleProject!.id}/playbooks`),
    enabled: form.kind === "playbook_sweep" && sampleProject != null,
  });
  const orgPlaybooks = (playbooks.data?.playbooks ?? []).filter(
    (p) => p.origin === "builtin" || (p.is_generic && p.organization_id === org.id),
  );

  const save = useMutation({
    mutationFn: () => {
      const body = {
        ...form,
        execution_mode: form.execution_mode || null,
        playbook_ids: form.kind === "playbook_sweep" ? form.playbook_ids : [],
      };
      return agent
        ? api.patch<RemediationAgent>(`/sre/agents/${agent.id}`, body)
        : api.post<RemediationAgent>(`/sre/organizations/${org.id}/agents`, body);
    },
    onSuccess: (saved) => {
      queryClient.invalidateQueries({ queryKey: ["agents", org.id] });
      onDone(saved);
    },
  });

  return (
    <form
      className="space-y-4 rounded-md border p-4"
      onSubmit={(event) => {
        event.preventDefault();
        save.mutate();
      }}
    >
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="Name">
          <Input
            required
            value={form.name}
            placeholder="Nightly sweep"
            onChange={(e) => setForm({ ...form, name: e.target.value })}
          />
        </Field>
        <Field label="Looks for" hint={kindDescriptions[form.kind]}>
          <Select
            value={form.kind}
            onChange={(e) => setForm({ ...form, kind: e.target.value as AgentKind })}
          >
            {(Object.keys(kindLabels) as AgentKind[]).map((k) => (
              <option key={k} value={k}>
                {kindLabels[k]}
              </option>
            ))}
          </Select>
        </Field>
        <Field label="Runs" hint={triggerDescriptions[form.trigger]}>
          <Select
            value={form.trigger}
            onChange={(e) => setForm({ ...form, trigger: e.target.value as AgentTrigger })}
          >
            {(Object.keys(triggerLabels) as AgentTrigger[]).map((t) => (
              <option key={t} value={t}>
                {triggerLabels[t]}
              </option>
            ))}
          </Select>
        </Field>
        {form.trigger === "schedule" && (
          <Field label="Cron schedule" hint="Five fields, in UTC. 0 3 * * * is every day at 03:00.">
            <Input
              required
              className="font-mono"
              value={form.schedule_cron}
              placeholder="0 3 * * *"
              onChange={(e) => setForm({ ...form, schedule_cron: e.target.value })}
            />
          </Field>
        )}
        {form.trigger === "branch_watch" && (
          <Field label="Branch pattern" hint="Glob, e.g. release/* or main.">
            <Input
              required
              className="font-mono"
              value={form.branch_pattern}
              placeholder="release/*"
              onChange={(e) => setForm({ ...form, branch_pattern: e.target.value })}
            />
          </Field>
        )}
        <Field
          label="Fixes can go as far as"
          hint="The limit for this agent. A finding without strong evidence gets a diagnosis only, and scan findings never merge on their own."
        >
          <Select
            value={form.execution_mode}
            onChange={(e) => setForm({ ...form, execution_mode: e.target.value as FormMode })}
          >
            {!agent && <option value="">{defaultModeLabel[form.kind]}</option>}
            <option value="advisory_only">Diagnosis only</option>
            <option value="draft_only">Draft PR</option>
          </Select>
        </Field>
        <Field label="Max findings per repo" hint="1 to 10, per scan.">
          <Input
            type="number"
            min={1}
            max={10}
            required
            value={form.max_findings_per_repo}
            onChange={(e) => setForm({ ...form, max_findings_per_repo: Number(e.target.value) })}
          />
        </Field>
        <Field
          label="Monthly token budget"
          hint="Tokens the scans may use each month. 0 means no limit. Fixes count toward each project's own cap."
        >
          <Input
            type="number"
            min={0}
            required
            value={form.monthly_token_budget}
            onChange={(e) => setForm({ ...form, monthly_token_budget: Number(e.target.value) })}
          />
        </Field>
      </div>

      <IdPicker
        legend="Repos"
        allLabel="Every project in the organization, including ones added later"
        items={orgProjects.map((p) => ({
          id: p.id,
          label: p.name,
          note: p.github_repo_owner ? `${p.github_repo_owner}/${p.github_repo_name}` : "no repo",
        }))}
        selected={form.project_ids}
        onChange={(project_ids) => setForm({ ...form, project_ids })}
      />

      {form.kind === "playbook_sweep" && (
        <IdPicker
          legend="Playbooks"
          allLabel="Every playbook that isn't failing"
          items={orgPlaybooks.map((p) => ({
            id: p.id,
            label: p.title,
            note: p.origin === "builtin" ? "built-in" : p.status,
          }))}
          selected={form.playbook_ids}
          onChange={(playbook_ids) => setForm({ ...form, playbook_ids })}
        />
      )}

      <label className="flex items-center gap-2 text-sm">
        <input
          type="checkbox"
          checked={form.enabled}
          onChange={(e) => setForm({ ...form, enabled: e.target.checked })}
        />
        <span className="font-medium">Enabled</span>
      </label>

      <ErrorText error={save.error} />
      <div className="flex gap-2">
        <Button type="submit" disabled={save.isPending}>
          {save.isPending ? "Saving..." : agent ? "Save changes" : "Create agent"}
        </Button>
        <Button type="button" variant="outline" onClick={() => onDone()}>
          Cancel
        </Button>
      </div>
    </form>
  );
}
