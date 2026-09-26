import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Copy, Plus, RefreshCw, ShieldAlert, ShieldCheck, X } from "lucide-react";
import { Link } from "react-router-dom";

import { api } from "@/api/client";
import type {
  ExecutionMode,
  GitHubInstallation,
  GitHubRepo,
  LLMConfig,
  Organization,
  PipelineStep,
  Platform,
  Project,
  ProjectCreated,
  ProjectCreateRequest,
  ProjectUpdateRequest,
  StepOverride,
  UptraceCredential,
  WebhookSecret,
} from "@/api/types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { ErrorText, Field } from "./form";
import { Monitoring, shareCandidates, useManagedUptrace } from "./Monitoring";

const executionModes: Record<ExecutionMode, string> = {
  advisory_only: "Advisory only: write a diagnosis, never touch code",
  draft_only: "Draft only: open a draft PR, wait for approval",
  autonomous: "Autonomous: open a ready PR (confirmed playbooks or runbooks only)",
};

const steps: Record<PipelineStep, string> = {
  anomaly_double_check: "Anomaly double-check",
  bug_classification: "Bug classification",
  playbook_similarity_judge: "Playbook similarity judge",
  playbook_creation: "Playbook creation",
  playbook_execution: "Playbook execution (agent)",
  repository_scan: "Repository scan (remediation agents)",
};

// Mirrors backend sre/llm/resolve.py: Jev only answers choice questions.
const JEV_STEPS: PipelineStep[] = [
  "anomaly_double_check",
  "bug_classification",
  "playbook_similarity_judge",
];

const canAdmin = (project: Project) => project.role === "owner" || project.role === "admin";
const isOrgAdmin = (org?: Organization) => org?.role === "owner" || org?.role === "admin";

export function useOrganizations() {
  return useQuery({
    queryKey: ["organizations"],
    queryFn: () => api.get<Organization[]>("/sre/organizations"),
  });
}

export function useUptraceCredentials(org?: Organization) {
  return useQuery({
    queryKey: ["uptrace-credentials", org?.id],
    queryFn: () => api.get<UptraceCredential[]>(`/sre/organizations/${org!.id}/uptrace-credentials`),
    enabled: isOrgAdmin(org),
  });
}

function useInstallations() {
  return useQuery({
    queryKey: ["github-installations"],
    queryFn: () => api.get<GitHubInstallation[]>("/sre/github/installations"),
  });
}

function useConfigs() {
  return useQuery({
    queryKey: ["llm-configs"],
    queryFn: () => api.get<LLMConfig[]>("/sre/llm-configs"),
  });
}

function CopyValue({ label, value }: { label: string; value: string }) {
  return (
    <Field label={label}>
      <div className="flex gap-2">
        <Input readOnly value={value} className="font-mono text-xs" />
        <Button
          type="button"
          variant="outline"
          size="icon"
          aria-label={`Copy ${label}`}
          onClick={() => navigator.clipboard?.writeText(value)}
        >
          <Copy />
        </Button>
      </div>
    </Field>
  );
}

function SecretPanel({
  secret,
  managed,
  onClose,
}: {
  secret: WebhookSecret;
  managed?: boolean;
  onClose: () => void;
}) {
  if (managed) {
    return (
      <div className="space-y-3 rounded-md border p-4">
        <div className="flex items-start justify-between gap-4">
          <p className="text-sm">
            Uptrace is being set up for you: no need to open it. Your DSN appears under{" "}
            <b>Monitoring</b> in the project below.
          </p>
          <Button variant="ghost" size="icon" onClick={onClose} aria-label="Dismiss">
            <X />
          </Button>
        </div>
        <details className="text-sm">
          <summary className="cursor-pointer text-muted-foreground">
            Calling the webhook from another tool too? (shown only once)
          </summary>
          <div className="mt-3 space-y-3">
            <CopyValue label="Webhook URL" value={secret.webhook_url} />
            <CopyValue
              label="Secret (X-SRE-Webhook-Secret header, or HMAC via X-SRE-Signature)"
              value={secret.webhook_secret}
            />
          </div>
        </details>
      </div>
    );
  }
  return (
    <div className="space-y-3 rounded-md border border-amber-500/60 bg-amber-50 dark:bg-amber-500/10 p-4">
      <div className="flex items-start justify-between gap-4">
        <p className="text-sm font-medium">
          Connect Uptrace. This is shown only once, so copy it now.
        </p>
        <Button variant="ghost" size="icon" onClick={onClose} aria-label="Dismiss">
          <X />
        </Button>
      </div>
      <CopyValue label="Uptrace webhook URL (includes the secret)" value={secret.uptrace_webhook_url} />
      <ol className="list-decimal space-y-1 pl-5 text-sm text-muted-foreground">
        <li>
          In Uptrace, open Alerting → Notification channels → New channel → <b>Webhook</b>.
        </li>
        <li>Paste the URL above as the Webhook URL and save.</li>
        <li>
          Select that channel on the error monitors you want fixed. Each new alert becomes one
          incident here, and closed alerts are ignored.
        </li>
      </ol>
      <details className="text-sm">
        <summary className="cursor-pointer text-muted-foreground">
          Calling from another tool instead?
        </summary>
        <div className="mt-3 space-y-3">
          <CopyValue label="Webhook URL" value={secret.webhook_url} />
          <CopyValue
            label="Secret (X-SRE-Webhook-Secret header, or HMAC via X-SRE-Signature)"
            value={secret.webhook_secret}
          />
        </div>
      </details>
    </div>
  );
}

type RepoValue = { installation_id: string; owner: string; name: string; branch: string };

/** Pick one of *your* connected installations, then a repo it can reach. */
function RepoPicker({
  value,
  onChange,
  disabled,
}: {
  value: RepoValue;
  onChange: (value: RepoValue) => void;
  disabled?: boolean;
}) {
  const installations = useInstallations();
  const current = installations.data?.find((i) => i.installation_id === value.installation_id);
  const repos = useQuery({
    queryKey: ["github-repos", current?.id],
    queryFn: () => api.get<GitHubRepo[]>(`/sre/github/installations/${current!.id}/repos`),
    enabled: current != null,
  });
  const repoKey = value.owner ? `${value.owner}/${value.name}` : "";
  const repoListed = repos.data?.some((r) => `${r.owner}/${r.name}` === repoKey);

  if (installations.data && installations.data.length === 0 && !value.installation_id) {
    return (
      <p className="text-sm text-muted-foreground sm:col-span-2">
        No GitHub installations connected.{" "}
        <Link to="/settings?tab=github" className="font-medium underline">
          Connect GitHub
        </Link>{" "}
        first.
      </p>
    );
  }

  return (
    <>
      <Field label="GitHub installation">
        <Select
          value={value.installation_id}
          disabled={disabled}
          required
          onChange={(e) =>
            onChange({ installation_id: e.target.value, owner: "", name: "", branch: "" })
          }
        >
          <option value="">Choose...</option>
          {value.installation_id && !current && (
            <option value={value.installation_id}>
              {value.installation_id} (not connected by you)
            </option>
          )}
          {installations.data?.map((i) => (
            <option key={i.id} value={i.installation_id}>
              {i.account_login}
            </option>
          ))}
        </Select>
      </Field>
      <Field
        label="Repository"
        hint={repos.isError ? "Could not list repositories from GitHub." : undefined}
      >
        <Select
          value={repoKey}
          disabled={disabled || !value.installation_id}
          required
          onChange={(e) => {
            const repo = repos.data?.find((r) => `${r.owner}/${r.name}` === e.target.value);
            if (repo) {
              onChange({ ...value, owner: repo.owner, name: repo.name, branch: repo.default_branch });
            }
          }}
        >
          <option value="">{repos.isLoading ? "Loading..." : "Choose..."}</option>
          {repoKey && !repoListed && <option value={repoKey}>{repoKey}</option>}
          {repos.data?.map((r) => (
            <option key={`${r.owner}/${r.name}`} value={`${r.owner}/${r.name}`}>
              {r.owner}/{r.name}
              {r.private ? " (private)" : ""}
            </option>
          ))}
        </Select>
      </Field>
    </>
  );
}

type FormState = {
  name: string;
  repo: RepoValue;
  default_execution_mode: ExecutionMode;
  uptrace_source_id: string;
  generate_tests: boolean;
  organization_id: number | null;
  uptrace_credential_id: number | null;
  service_names: string;
  share_with: number | "";
};

function parseServiceNames(value: string): string[] {
  return value
    .split(",")
    .map((name) => name.trim())
    .filter(Boolean);
}

function formFor(project?: Project): FormState {
  return {
    name: project?.name ?? "",
    repo: {
      installation_id: project?.github_installation_id ?? "",
      owner: project?.github_repo_owner ?? "",
      name: project?.github_repo_name ?? "",
      branch: project?.github_default_branch ?? "",
    },
    default_execution_mode: project?.default_execution_mode ?? "draft_only",
    uptrace_source_id: project?.uptrace_source_id ?? "",
    generate_tests: project?.generate_tests ?? true,
    organization_id: project?.organization_id ?? null,
    uptrace_credential_id: project?.uptrace_credential_id ?? null,
    service_names: (project?.service_names ?? []).join(", "),
    share_with: "",
  };
}

function ProjectForm({
  project,
  onSaved,
  onCancel,
}: {
  project?: Project;
  onSaved: (result: Project | ProjectCreated) => void;
  onCancel?: () => void;
}) {
  const queryClient = useQueryClient();
  const [form, setForm] = useState<FormState>(() => formFor(project));
  const isOwner = !project || project.role === "owner";
  const readOnly = project != null && !canAdmin(project);
  const orgs = useOrganizations();
  const adminOrgs = (orgs.data ?? []).filter(isOrgAdmin);
  const org = (orgs.data ?? []).find((o) => o.id === form.organization_id);
  const credentials = useUptraceCredentials(project ? org : undefined);
  const managedUptrace = useManagedUptrace();
  const allProjects = useQuery({
    queryKey: ["projects"],
    queryFn: () => api.get<Project[]>("/sre/projects"),
    enabled: !project,
  });
  // New projects get a platform-managed Uptrace; hand-pinned ones keep their own.
  const managed = project ? project.uptrace_managed : managedUptrace.data?.enabled === true;
  const candidates = shareCandidates(allProjects.data);

  const save = useMutation({
    mutationFn: () => {
      const fields = {
        name: form.name,
        github_installation_id: form.repo.installation_id,
        github_repo_owner: form.repo.owner,
        github_repo_name: form.repo.name,
        github_default_branch: form.repo.branch || "main",
        default_execution_mode: form.default_execution_mode,
        uptrace_source_id: form.uptrace_source_id,
        generate_tests: form.generate_tests,
        organization_id: form.organization_id,
      };
      const serviceNames = parseServiceNames(form.service_names);
      if (!project) {
        return api.post<ProjectCreated>("/sre/projects", {
          ...fields,
          service_names: serviceNames,
          uptrace_share_with_project_id: managed && form.share_with ? form.share_with : null,
        } as ProjectCreateRequest);
      }
      const editFields = { ...fields, uptrace_credential_id: form.uptrace_credential_id };
      // Send only what changed: admins may not touch the owner-only GitHub/Uptrace fields.
      const changes: ProjectUpdateRequest = {};
      for (const [key, value] of Object.entries(editFields) as [keyof typeof editFields, unknown][]) {
        // A field the server didn't send counts as null, not as a change.
        if ((project[key] ?? null) !== (value ?? null)) (changes as Record<string, unknown>)[key] = value;
      }
      if (serviceNames.join(",") !== project.service_names.join(",")) {
        changes.service_names = serviceNames;
      }
      // The platform owns a managed project's pin.
      if (project.uptrace_managed) delete changes.uptrace_source_id;
      return api.patch<Project>(`/sre/projects/${project.id}`, changes);
    },
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ["projects"] });
      onSaved(result);
    },
  });

  return (
    <form
      className="space-y-4"
      onSubmit={(event) => {
        event.preventDefault();
        save.mutate();
      }}
    >
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="Name">
          <Input
            value={form.name}
            disabled={readOnly}
            required
            onChange={(e) => setForm({ ...form, name: e.target.value })}
          />
        </Field>
        <Field label="Execution mode">
          <Select
            value={form.default_execution_mode}
            disabled={readOnly}
            onChange={(e) =>
              setForm({ ...form, default_execution_mode: e.target.value as ExecutionMode })
            }
          >
            {Object.entries(executionModes).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </Select>
        </Field>
        <RepoPicker
          value={form.repo}
          disabled={!isOwner}
          onChange={(repo) => setForm({ ...form, repo })}
        />
        <Field label="Default branch" hint="The branch the agent clones and targets.">
          <Input
            value={form.repo.branch}
            disabled={!isOwner}
            placeholder="main"
            onChange={(e) => setForm({ ...form, repo: { ...form.repo, branch: e.target.value } })}
          />
        </Field>
        <Field
          label="Service names"
          hint={
            <>
              The <code>service.name</code> this repo's app reports (<code>OTEL_SERVICE_NAME</code>),
              comma-separated. Needed to share an Uptrace project, and used to link services.
            </>
          }
        >
          <Input
            value={form.service_names}
            disabled={readOnly}
            placeholder={form.repo.name || "my-service"}
            onChange={(e) => setForm({ ...form, service_names: e.target.value })}
          />
        </Field>
        {!project && managed && candidates.length > 0 && (
          <Field
            label="Monitoring"
            hint="Share an existing project's Uptrace project when their services call each other, to see one trace across both. Both need service names."
          >
            <Select
              value={form.share_with}
              onChange={(e) =>
                setForm({ ...form, share_with: e.target.value ? Number(e.target.value) : "" })
              }
            >
              <option value="">Its own Uptrace project (set up for you)</option>
              {candidates.map((p) => (
                <option key={p.id} value={p.id}>
                  Share {p.name}'s Uptrace project
                </option>
              ))}
            </Select>
          </Field>
        )}
        {!managed && (
        <Field
          label="Uptrace project"
          hint={
            form.uptrace_source_id
              ? "Only alerts from this Uptrace project are accepted. Clear it and save to re-pin on the next alert."
              : "Set automatically by the first alert (e.g. app.uptrace.dev/1). Later alerts from any other Uptrace project are rejected."
          }
        >
          <Input
            value={form.uptrace_source_id}
            disabled={!isOwner}
            placeholder="Not pinned yet"
            onChange={(e) => setForm({ ...form, uptrace_source_id: e.target.value })}
          />
        </Field>
        )}
        {adminOrgs.length > 1 || (project && org) ? (
          <Field
            label="Organization"
            hint="Its generic playbooks are shared by every project in the organization."
          >
            <Select
              value={form.organization_id ?? ""}
              disabled={!isOwner || adminOrgs.length < 2}
              onChange={(e) =>
                setForm({ ...form, organization_id: e.target.value ? Number(e.target.value) : null,
                          uptrace_credential_id: null })
              }
            >
              {!project && <option value="">Your personal organization</option>}
              {(adminOrgs.some((o) => o.id === org?.id) || !org ? adminOrgs : [org, ...adminOrgs]).map(
                (o) => (
                  <option key={o.id} value={o.id}>
                    {o.name}
                  </option>
                ),
              )}
            </Select>
          </Field>
        ) : null}
        {project && !project.uptrace_managed && (
          <Field
            label="Uptrace credential"
            hint={
              project.uptrace_fetch_ready
                ? "The agent reads each alert's exception and stack trace from Uptrace."
                : "No credential found: the agent only sees the alert's name. Add one in Settings → Uptrace."
            }
          >
            <Select
              value={form.uptrace_credential_id ?? ""}
              disabled={!isOwner || !credentials.data}
              onChange={(e) =>
                setForm({
                  ...form,
                  uptrace_credential_id: e.target.value ? Number(e.target.value) : null,
                })
              }
            >
              <option value="">Automatic (the organization's credential for this host)</option>
              {(credentials.data ?? []).map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name} ({c.host})
                </option>
              ))}
            </Select>
          </Field>
        )}
      </div>
      <label className="flex items-start gap-2 text-sm">
        <input
          type="checkbox"
          className="mt-1"
          checked={form.generate_tests}
          disabled={readOnly}
          onChange={(e) => setForm({ ...form, generate_tests: e.target.checked })}
        />
        <span>
          <span className="font-medium">Generate tests</span>
          <span className="block text-xs text-muted-foreground">
            The fix agent writes a test covering each fix. Turn this off for cheaper runs: it then
            only runs the repo's existing tests. Each incident on the dashboard shows which setting
            it ran with, so you can compare the tokens.
          </span>
        </span>
      </label>
      {project && !isOwner && canAdmin(project) && (
        <p className="text-xs text-muted-foreground">Only owners can change the GitHub wiring.</p>
      )}
      <ErrorText error={save.error} />
      {!readOnly && (
        <div className="flex gap-2">
          <Button type="submit" disabled={save.isPending}>
            {save.isPending ? "Saving..." : project ? "Save project" : "Create project"}
          </Button>
          {onCancel && (
            <Button type="button" variant="outline" onClick={onCancel}>
              Cancel
            </Button>
          )}
        </div>
      )}
    </form>
  );
}

function ProjectModels({ project }: { project: Project }) {
  const queryClient = useQueryClient();
  const configs = useConfigs();
  const overrides = useQuery({
    queryKey: ["step-overrides", project.id],
    queryFn: () => api.get<StepOverride[]>(`/sre/projects/${project.id}/step-overrides`),
  });
  const platform = useQuery({
    queryKey: ["platform"],
    queryFn: () => api.get<Platform>("/sre/platform"),
  });
  const editable = canAdmin(project);

  const setDefault = useMutation({
    mutationFn: (id: number | null) =>
      api.patch<Project>(`/sre/projects/${project.id}`, { default_llm_config_id: id }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["projects"] }),
  });
  const setOverride = useMutation({
    mutationFn: ({ step, id }: { step: PipelineStep; id: number | null }) =>
      api.put<StepOverride[]>(`/sre/projects/${project.id}/step-overrides`, {
        overrides: { [step]: id },
      }),
    onSuccess: (data) => queryClient.setQueryData(["step-overrides", project.id], data),
  });

  const mine = configs.data ?? [];
  const defaultId = project.default_llm_config_id;
  const defaultConfig = mine.find((c) => c.id === defaultId);
  const overrideFor = (step: PipelineStep) => overrides.data?.find((o) => o.step === step);
  const jevDefaultGaps =
    defaultConfig?.provider === "jev_cloudflare"
      ? (Object.keys(steps) as PipelineStep[]).filter(
          (s) => !JEV_STEPS.includes(s) && !overrideFor(s),
        )
      : [];

  const company = platform.data?.available ? platform.data : null;
  const companyLabel = company
    ? `Company default (${company.triage_model === "jev" ? "Jev" : company.triage_model} + ${company.strong_model})`
    : "None";
  const cap = company?.monthly_token_cap ?? 0;
  const used = project.platform_tokens_this_month;

  return (
    <div className="space-y-4">
      <Field
        label="Default model"
        hint={
          company
            ? "Used for every step without an override. The company default runs on our keys at no setup; pick your own config to use your key instead."
            : "Used for every step without an override. You can pick from your own configs."
        }
      >
        <Select
          value={defaultId?.toString() ?? ""}
          disabled={!editable || setDefault.isPending}
          onChange={(e) => setDefault.mutate(e.target.value ? Number(e.target.value) : null)}
        >
          <option value="">{companyLabel}</option>
          {defaultId != null && !defaultConfig && (
            <option value={defaultId}>Another member's config (#{defaultId})</option>
          )}
          {mine.map((c) => (
            <option key={c.id} value={c.id}>
              {c.name} ({c.model || c.provider})
            </option>
          ))}
        </Select>
      </Field>
      {defaultId == null && !company && platform.isSuccess && (
        <p className="text-sm text-amber-700 dark:text-amber-300">
          No default model: incidents fail unless every step has an override.
        </p>
      )}
      {company && (defaultId == null || used > 0) && (
        <div className="space-y-1 text-sm" data-testid="company-usage">
          <div className="flex justify-between text-muted-foreground">
            <span>Company model this month</span>
            <span className="font-mono text-xs">
              {used.toLocaleString()}
              {cap ? ` / ${cap.toLocaleString()}` : ""} tokens
            </span>
          </div>
          {cap > 0 && (
            <div className="h-1.5 overflow-hidden rounded-full bg-muted">
              <div
                className={`h-full ${used >= cap ? "bg-destructive" : "bg-primary"}`}
                style={{ width: `${Math.min(100, (used / cap) * 100)}%` }}
              />
            </div>
          )}
          {cap > 0 && used >= cap && (
            <p className="text-destructive">
              Monthly limit reached: incidents stop until next month or until you pick your own
              model.
            </p>
          )}
        </div>
      )}
      {jevDefaultGaps.length > 0 && (
        <p className="text-sm text-amber-700 dark:text-amber-300">
          The default is a Jev config, which can't run{" "}
          {jevDefaultGaps.map((s) => steps[s].toLowerCase()).join(", ")}. Set an override for
          those.
        </p>
      )}
      <div className="rounded-md border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Pipeline step</TableHead>
              <TableHead>Model</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {(Object.entries(steps) as [PipelineStep, string][]).map(([step, label]) => {
              const override = overrideFor(step);
              const overrideIsMine = mine.some((c) => c.id === override?.llm_config_id);
              return (
                <TableRow key={step}>
                  <TableCell>{label}</TableCell>
                  <TableCell>
                    <Select
                      aria-label={`Model for ${label}`}
                      value={override?.llm_config_id.toString() ?? ""}
                      disabled={!editable || setOverride.isPending}
                      onChange={(e) =>
                        setOverride.mutate({
                          step,
                          id: e.target.value ? Number(e.target.value) : null,
                        })
                      }
                    >
                      <option value="">Project default</option>
                      {override && !overrideIsMine && (
                        <option value={override.llm_config_id}>{override.llm_config_name}</option>
                      )}
                      {mine.map((c) => (
                        <option
                          key={c.id}
                          value={c.id}
                          disabled={c.provider === "jev_cloudflare" && !JEV_STEPS.includes(step)}
                        >
                          {c.name} ({c.model || c.provider})
                        </option>
                      ))}
                    </Select>
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      </div>
      {mine.length === 0 && editable && !company && (
        <p className="text-sm text-muted-foreground">
          You have no model configs.{" "}
          <Link to="/settings?tab=models" className="font-medium underline">
            Add one
          </Link>
          .
        </p>
      )}
      <ErrorText error={setDefault.error ?? setOverride.error} />
    </div>
  );
}

function ProjectDetail({
  project,
  projects,
  onClose,
}: {
  project: Project;
  projects?: Project[];
  onClose: () => void;
}) {
  const managed = useManagedUptrace();
  const [secret, setSecret] = useState<WebhookSecret | null>(null);
  const rotate = useMutation({
    mutationFn: () => api.post<WebhookSecret>(`/sre/projects/${project.id}/webhook-secret/rotate`),
    onSuccess: setSecret,
  });

  return (
    <Card>
      <CardHeader className="flex flex-row items-start justify-between space-y-0">
        <div className="space-y-1.5">
          <CardTitle>{project.name}</CardTitle>
          <CardDescription>
            {project.github_repo_owner}/{project.github_repo_name} · you are {project.role}
          </CardDescription>
        </div>
        <Button variant="ghost" size="icon" onClick={onClose} aria-label="Close project">
          <X />
        </Button>
      </CardHeader>
      <CardContent className="space-y-8">
        <section className="space-y-3">
          <h3 className="text-sm font-semibold">Project</h3>
          <ProjectForm key={project.id} project={project} onSaved={() => undefined} />
        </section>
        <section className="space-y-3">
          <h3 className="text-sm font-semibold">Models for this project</h3>
          <ProjectModels project={project} />
        </section>
        {(project.uptrace_managed || managed.data?.enabled) && (
          <section className="space-y-3">
            <h3 className="text-sm font-semibold">Monitoring</h3>
            <Monitoring project={project} projects={projects} />
          </section>
        )}
        {project.role === "owner" && (
          <section className="space-y-3">
            <h3 className="text-sm font-semibold">
              {project.uptrace_managed ? "Webhook secret" : "Uptrace webhook"}
            </h3>
            {project.uptrace_managed && !secret && (
              <p className="text-sm text-muted-foreground">
                Rotating it updates the Uptrace channel automatically.
              </p>
            )}
            {secret ? (
              <SecretPanel
                secret={secret}
                managed={project.uptrace_managed}
                onClose={() => setSecret(null)}
              />
            ) : (
              <Button
                variant="outline"
                onClick={() => {
                  if (window.confirm("Rotate the webhook secret? The old one stops working.")) {
                    rotate.mutate();
                  }
                }}
                disabled={rotate.isPending}
              >
                <RefreshCw /> Rotate webhook secret
              </Button>
            )}
            <ErrorText error={rotate.error} />
          </section>
        )}
      </CardContent>
    </Card>
  );
}

export function ProjectsTab() {
  const [selected, setSelected] = useState<number | "new" | null>(null);
  const [created, setCreated] = useState<ProjectCreated | null>(null);
  const { data: projects, isLoading, isError } = useQuery({
    queryKey: ["projects"],
    queryFn: () => api.get<Project[]>("/sre/projects"),
    // Managed Uptrace is set up in the background: refresh until it's done.
    refetchInterval: (query) =>
      query.state.data?.some((p) => p.uptrace_status === "provisioning") ? 3000 : false,
  });
  const project = projects?.find((p) => p.id === selected);

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader className="flex flex-row items-start justify-between space-y-0">
          <div className="space-y-1.5">
            <CardTitle>Projects</CardTitle>
            <CardDescription>Each project watches one repository.</CardDescription>
          </div>
          {selected !== "new" && (
            <Button onClick={() => setSelected("new")}>
              <Plus /> New project
            </Button>
          )}
        </CardHeader>
        <CardContent className="space-y-4">
          {created && (
            <SecretPanel
              secret={created}
              managed={created.uptrace_managed}
              onClose={() => setCreated(null)}
            />
          )}
          {selected === "new" && (
            <div className="rounded-md border p-4">
              <ProjectForm
                onCancel={() => setSelected(null)}
                onSaved={(result) => {
                  setCreated(result as ProjectCreated);
                  setSelected(result.id);
                }}
              />
            </div>
          )}
          {isLoading && <p className="text-sm text-muted-foreground">Loading...</p>}
          {isError && <p className="text-sm text-destructive">Could not load projects.</p>}
          {projects && projects.length === 0 && selected !== "new" && (
            <p className="text-sm text-muted-foreground">No projects yet.</p>
          )}
          {projects && projects.length > 0 && (
            <div className="rounded-md border">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Name</TableHead>
                    <TableHead>Repository</TableHead>
                    <TableHead>Organization</TableHead>
                    <TableHead>Mode</TableHead>
                    <TableHead>Role</TableHead>
                    <TableHead>GitHub</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {projects.map((p) => (
                    <TableRow
                      key={p.id}
                      className="cursor-pointer"
                      data-state={p.id === selected ? "selected" : undefined}
                      onClick={() => setSelected(p.id === selected ? null : p.id)}
                    >
                      <TableCell className="font-medium">{p.name}</TableCell>
                      <TableCell className="font-mono text-xs">
                        {p.github_repo_owner}/{p.github_repo_name}
                      </TableCell>
                      <TableCell className="text-sm">{p.organization_name}</TableCell>
                      <TableCell>{p.default_execution_mode.replace(/_/g, " ")}</TableCell>
                      <TableCell>
                        <Badge variant="outline">{p.role}</Badge>
                      </TableCell>
                      <TableCell>
                        {p.github_verified ? (
                          <span className="inline-flex items-center gap-1 text-sm text-emerald-700 dark:text-emerald-300">
                            <ShieldCheck className="h-4 w-4" /> Verified
                          </span>
                        ) : (
                          <span
                            className="inline-flex items-center gap-1 text-sm text-amber-700 dark:text-amber-300"
                            title="No owner of this project has connected its GitHub installation. An owner can fix this in the GitHub tab."
                          >
                            <ShieldAlert className="h-4 w-4" /> Unverified
                          </span>
                        )}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
        </CardContent>
      </Card>
      {project && (
        <ProjectDetail project={project} projects={projects} onClose={() => setSelected(null)} />
      )}
    </div>
  );
}
