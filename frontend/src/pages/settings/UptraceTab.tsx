import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Plus, Trash2 } from "lucide-react";

import { api } from "@/api/client";
import type {
  Organization,
  UptraceCredential,
  UptraceCredentialRequest,
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
import { useManagedUptrace } from "./Monitoring";
import { useOrganizations, useUptraceCredentials } from "./ProjectsTab";

const emptyForm: UptraceCredentialRequest = { name: "", host: "", api_base_url: "", token: "" };

function CredentialForm({ org, onDone }: { org: Organization; onDone: () => void }) {
  const queryClient = useQueryClient();
  const [form, setForm] = useState(emptyForm);
  const create = useMutation({
    mutationFn: () =>
      api.post<UptraceCredential>(`/sre/organizations/${org.id}/uptrace-credentials`, {
        ...form,
        // Self-hosted Uptrace serves its API on the same host as the UI.
        api_base_url: form.api_base_url || `https://${form.host.replace(/^https?:\/\//, "")}`,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["uptrace-credentials", org.id] });
      queryClient.invalidateQueries({ queryKey: ["projects"] });
      onDone();
    },
  });
  return (
    <form
      className="space-y-4 rounded-md border p-4"
      onSubmit={(event) => {
        event.preventDefault();
        create.mutate();
      }}
    >
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="Name">
          <Input required value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
        </Field>
        <Field label="Uptrace host" hint="As in your alert links, e.g. uptrace.buggly.dev">
          <Input required value={form.host} onChange={(e) => setForm({ ...form, host: e.target.value })} />
        </Field>
        <Field label="API URL" hint="Leave blank for self-hosted (same host as the UI).">
          <Input
            value={form.api_base_url}
            placeholder="https://<host>"
            onChange={(e) => setForm({ ...form, api_base_url: e.target.value })}
          />
        </Field>
        <Field label="API token" hint="A user API token from Uptrace. It's never shown again.">
          <Input
            required
            type="password"
            autoComplete="off"
            value={form.token}
            onChange={(e) => setForm({ ...form, token: e.target.value })}
          />
        </Field>
      </div>
      <ErrorText error={create.error} />
      <div className="flex gap-2">
        <Button type="submit" disabled={create.isPending}>
          {create.isPending ? "Saving..." : "Add credential"}
        </Button>
        <Button type="button" variant="outline" onClick={onDone}>
          Cancel
        </Button>
      </div>
    </form>
  );
}

export function UptraceTab() {
  const queryClient = useQueryClient();
  const orgs = useOrganizations();
  const adminOrgs = (orgs.data ?? []).filter((o) => o.role === "owner" || o.role === "admin");
  const [orgId, setOrgId] = useState<number | null>(null);
  const org = adminOrgs.find((o) => o.id === orgId) ?? adminOrgs[0];
  const credentials = useUptraceCredentials(org);
  const [adding, setAdding] = useState(false);
  const managed = useManagedUptrace();
  const remove = useMutation({
    mutationFn: (id: number) => api.delete(`/sre/uptrace-credentials/${id}`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["uptrace-credentials", org?.id] });
      queryClient.invalidateQueries({ queryKey: ["projects"] });
    },
  });

  return (
    <Card>
      <CardHeader className="flex flex-row items-start justify-between gap-4 space-y-0">
        <div className="space-y-1">
          <CardTitle>Uptrace</CardTitle>
          <CardDescription>
            With a credential, the agent reads each alert's exception, stack trace and service
            from Uptrace instead of just the alert's name. One per organization and Uptrace host.
            {managed.data?.enabled && (
              <>
                {" "}
                Projects whose Uptrace the platform set up (see Monitoring on each project) need
                none: this is only for an Uptrace you connected yourself.
              </>
            )}
          </CardDescription>
        </div>
        {org && !adding && (
          <Button onClick={() => setAdding(true)}>
            <Plus /> Add credential
          </Button>
        )}
      </CardHeader>
      <CardContent className="space-y-4">
        {orgs.isLoading && <p className="text-sm text-muted-foreground">Loading...</p>}
        {adminOrgs.length > 1 && (
          <Field label="Organization">
            <Select value={org?.id ?? ""} onChange={(e) => setOrgId(Number(e.target.value))}>
              {adminOrgs.map((o) => (
                <option key={o.id} value={o.id}>
                  {o.name}
                </option>
              ))}
            </Select>
          </Field>
        )}
        {org && adding && <CredentialForm org={org} onDone={() => setAdding(false)} />}
        {credentials.isError && (
          <p className="text-sm text-destructive">Could not load credentials.</p>
        )}
        {credentials.data && credentials.data.length === 0 && !adding && (
          <p className="text-sm text-muted-foreground">No Uptrace credentials yet.</p>
        )}
        {credentials.data && credentials.data.length > 0 && (
          <div className="rounded-md border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Name</TableHead>
                  <TableHead>Host</TableHead>
                  <TableHead>API</TableHead>
                  <TableHead>Token</TableHead>
                  <TableHead />
                </TableRow>
              </TableHeader>
              <TableBody>
                {credentials.data.map((c) => (
                  <TableRow key={c.id}>
                    <TableCell className="font-medium">{c.name}</TableCell>
                    <TableCell className="font-mono text-xs">{c.host}</TableCell>
                    <TableCell className="font-mono text-xs">{c.api_base_url}</TableCell>
                    <TableCell>
                      <Badge variant={c.has_token ? "outline" : "destructive"}>
                        {c.has_token ? "set" : "missing"}
                      </Badge>
                    </TableCell>
                    <TableCell className="text-right">
                      <Button
                        variant="ghost"
                        size="icon"
                        aria-label={`Delete ${c.name}`}
                        onClick={() => remove.mutate(c.id)}
                      >
                        <Trash2 />
                      </Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
        <ErrorText error={remove.error} />
      </CardContent>
    </Card>
  );
}
