import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Copy, Loader2, RefreshCw } from "lucide-react";

import { api } from "@/api/client";
import type { ManagedUptrace, Project, UptraceSetupRequest } from "@/api/types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";

import { ErrorText, Field } from "./form";

/** Whether this server sets up Uptrace for projects itself (managed Uptrace). */
export function useManagedUptrace() {
  return useQuery({
    queryKey: ["uptrace-managed"],
    queryFn: () => api.get<ManagedUptrace>("/sre/uptrace/managed"),
    staleTime: Infinity,
  });
}

/** Managed projects another project could share an Uptrace project with. */
export function shareCandidates(projects: Project[] | undefined, exclude?: Project) {
  return (projects ?? []).filter(
    (p) =>
      p.id !== exclude?.id &&
      p.uptrace_managed &&
      p.uptrace_project_id != null &&
      (p.role === "owner" || p.role === "admin") &&
      (exclude == null || p.organization_id === exclude.organization_id),
  );
}

function CopyLine({ label, value }: { label: string; value: string }) {
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

const statusBadge: Record<string, { label: string; className: string }> = {
  provisioning: { label: "Setting up", className: "text-amber-700 dark:text-amber-300" },
  ready: { label: "Ready", className: "text-emerald-700 dark:text-emerald-300" },
  error: { label: "Needs attention", className: "text-destructive" },
};

/** Where the project's telemetry goes and how to send it, when the platform manages Uptrace. */
export function Monitoring({ project, projects }: { project: Project; projects?: Project[] }) {
  const queryClient = useQueryClient();
  const managed = useManagedUptrace();
  const isOwner = project.role === "owner";
  const candidates = shareCandidates(projects, project);
  const [shareWith, setShareWith] = useState<number | "">("");

  const setup = useMutation({
    mutationFn: (body: UptraceSetupRequest) =>
      api.post<Project>(`/sre/projects/${project.id}/uptrace/setup`, body),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["projects"] }),
  });
  const currentShare = project.uptrace_shared_with[0]?.id ?? null;

  if (!project.uptrace_managed) {
    if (!managed.data?.enabled) return null;
    return (
      <div className="space-y-3 text-sm">
        <p className="text-muted-foreground">
          This project is connected to Uptrace by hand. Let the platform set it up instead: it
          creates the Uptrace project, the error alert and the webhook, and gives you a DSN. Nobody
          has to open Uptrace.
        </p>
        {isOwner ? (
          <SetupControls
            candidates={candidates}
            shareWith={shareWith}
            setShareWith={setShareWith}
            pending={setup.isPending}
            label="Let the platform manage Uptrace"
            onSubmit={() => setup.mutate({ share_with_project_id: shareWith || null })}
          />
        ) : (
          <p className="text-muted-foreground">An owner can switch this on.</p>
        )}
        <ErrorText error={setup.error} />
      </div>
    );
  }

  const badge = statusBadge[project.uptrace_status] ?? statusBadge.provisioning;
  const serviceName = project.service_names[0] || project.github_repo_name;

  return (
    <div className="space-y-4 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant="outline" className={badge.className}>
          {project.uptrace_status === "provisioning" && <Loader2 className="h-3 w-3 animate-spin" />}
          {badge.label}
        </Badge>
        <span className="text-muted-foreground">
          Managed by the platform
          {project.uptrace_source_id ? ` (Uptrace project ${project.uptrace_source_id})` : ""}.
        </span>
      </div>

      {project.uptrace_status === "error" && (
        <div className="space-y-2 rounded-md border border-destructive/40 p-3">
          <p className="text-destructive">{project.uptrace_error || "Setup failed."}</p>
          {isOwner && (
            <Button
              variant="outline"
              size="sm"
              disabled={setup.isPending}
              onClick={() => setup.mutate({ share_with_project_id: currentShare })}
            >
              <RefreshCw /> Try again
            </Button>
          )}
        </div>
      )}

      {project.uptrace_status === "provisioning" && (
        <p className="text-muted-foreground">
          Creating the Uptrace project, error alert and webhook. This takes a few seconds.
        </p>
      )}

      {project.uptrace_dsn ? (
        <div className="space-y-3">
          <CopyLine label="DSN (where your app sends telemetry)" value={project.uptrace_dsn} />
          <div className="space-y-1">
            <p className="font-medium">Send your app's errors here</p>
            <ol className="list-decimal space-y-1 pl-5 text-muted-foreground">
              <li>
                Set <code>UPTRACE_DSN</code> to the DSN above and <code>OTEL_SERVICE_NAME</code> to{" "}
                <code>{serviceName}</code>.
              </li>
              <li>
                Export OpenTelemetry over <b>OTLP/HTTP</b> to{" "}
                <code>{managed.data?.url || "the Uptrace URL"}/v1/traces</code> with the header{" "}
                <code>uptrace-dsn: &lt;DSN&gt;</code>. (Uptrace's own Python SDK only speaks gRPC,
                which this Uptrace doesn't accept.)
              </li>
              <li>
                That's it: each new error opens an incident here within about a minute and a half,
                and the agent takes it from there.
              </li>
            </ol>
          </div>
        </div>
      ) : (
        project.uptrace_status === "ready" && (
          <p className="text-muted-foreground">Ask an admin or owner for the DSN.</p>
        )
      )}

      {project.uptrace_shared_with.length > 0 && (
        <p className="text-muted-foreground">
          Shares its Uptrace project with{" "}
          {project.uptrace_shared_with.map((p) => p.name).join(", ")}, so calls between them show
          up as one trace. Alerts are told apart by service name ({project.service_names.join(", ")}).
        </p>
      )}

      {isOwner && project.uptrace_status !== "provisioning" && (
        <details>
          <summary className="cursor-pointer text-muted-foreground">Change sharing</summary>
          <div className="mt-3">
            <SetupControls
              candidates={candidates.filter((p) => p.uptrace_project_id !== project.uptrace_project_id)}
              shareWith={shareWith}
              setShareWith={setShareWith}
              pending={setup.isPending}
              label="Apply"
              ownLabel={
                project.uptrace_shared_with.length > 0
                  ? "Stop sharing: its own Uptrace project"
                  : "Keep its own Uptrace project"
              }
              onSubmit={() => setup.mutate({ share_with_project_id: shareWith || null })}
            />
          </div>
        </details>
      )}
      <ErrorText error={setup.error} />
    </div>
  );
}

function SetupControls({
  candidates,
  shareWith,
  setShareWith,
  pending,
  label,
  ownLabel = "Its own Uptrace project",
  onSubmit,
}: {
  candidates: Project[];
  shareWith: number | "";
  setShareWith: (value: number | "") => void;
  pending: boolean;
  label: string;
  ownLabel?: string;
  onSubmit: () => void;
}) {
  return (
    <div className="flex flex-wrap items-end gap-2">
      {candidates.length > 0 && (
        <Field
          label="Uptrace project"
          hint="Share with a project whose services call this one, to see one trace across both. Both need service names."
        >
          <Select
            value={shareWith}
            onChange={(e) => setShareWith(e.target.value ? Number(e.target.value) : "")}
          >
            <option value="">{ownLabel}</option>
            {candidates.map((p) => (
              <option key={p.id} value={p.id}>
                Share with {p.name}
              </option>
            ))}
          </Select>
        </Field>
      )}
      <Button type="button" disabled={pending} onClick={onSubmit}>
        {pending ? "Setting up..." : label}
      </Button>
    </div>
  );
}
