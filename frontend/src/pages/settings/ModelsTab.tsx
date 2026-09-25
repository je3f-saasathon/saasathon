import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Pencil, Plus, Trash2 } from "lucide-react";

import { api } from "@/api/client";
import type {
  LLMConfig,
  LLMConfigRequest,
  LLMConfigUpdateRequest,
  LLMProvider,
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

export const providerLabels: Record<LLMProvider, string> = {
  anthropic: "Anthropic",
  openai: "OpenAI",
  self_hosted: "Self-hosted (OpenAI-compatible)",
  jev_cloudflare: "Jev (Cloudflare Workers AI)",
};

type FormState = {
  name: string;
  provider: LLMProvider;
  model: string;
  base_url: string;
  api_key: string;
  clear_key: boolean;
  max_tokens: string;
  temperature: string;
};

const emptyForm: FormState = {
  name: "",
  provider: "anthropic",
  model: "",
  base_url: "",
  api_key: "",
  clear_key: false,
  max_tokens: "",
  temperature: "",
};

function formFor(config: LLMConfig): FormState {
  const extra = config.extra_config as { max_tokens?: number; temperature?: number };
  return {
    ...emptyForm,
    name: config.name,
    provider: config.provider,
    model: config.model,
    base_url: config.base_url,
    max_tokens: extra.max_tokens?.toString() ?? "",
    temperature: extra.temperature?.toString() ?? "",
  };
}

function extraConfig(form: FormState, previous: Record<string, unknown> = {}) {
  const extra: Record<string, unknown> = { ...previous };
  delete extra.max_tokens;
  delete extra.temperature;
  if (form.max_tokens) extra.max_tokens = Number(form.max_tokens);
  if (form.temperature) extra.temperature = Number(form.temperature);
  return extra;
}

function ConfigForm({ editing, onDone }: { editing: LLMConfig | null; onDone: () => void }) {
  const queryClient = useQueryClient();
  const [form, setForm] = useState<FormState>(editing ? formFor(editing) : emptyForm);
  const set = (patch: Partial<FormState>) => setForm((f) => ({ ...f, ...patch }));

  const save = useMutation({
    mutationFn: () => {
      if (editing) {
        const body: LLMConfigUpdateRequest = {
          name: form.name,
          model: form.model,
          base_url: form.base_url,
          extra_config: extraConfig(form, editing.extra_config),
        };
        // Blank means "keep the stored key"; "" is only sent to clear it.
        if (form.clear_key) body.api_key = "";
        else if (form.api_key) body.api_key = form.api_key;
        return api.patch<LLMConfig>(`/sre/llm-configs/${editing.id}`, body);
      }
      const body: LLMConfigRequest = {
        name: form.name,
        provider: form.provider,
        model: form.model,
        base_url: form.base_url,
        api_key: form.api_key,
        extra_config: extraConfig(form),
      };
      return api.post<LLMConfig>("/sre/llm-configs", body);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["llm-configs"] });
      onDone();
    },
  });

  const isJev = form.provider === "jev_cloudflare";
  const needsBaseUrl = form.provider === "self_hosted";

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
          <Input value={form.name} onChange={(e) => set({ name: e.target.value })} required />
        </Field>
        <Field
          label="Provider"
          hint={editing ? "The provider can't change; create a new config instead." : undefined}
        >
          <Select
            value={form.provider}
            disabled={!!editing}
            onChange={(e) => set({ provider: e.target.value as LLMProvider })}
          >
            {Object.entries(providerLabels).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </Select>
        </Field>
        <Field label="Model" hint={isJev ? "Optional for Jev." : "e.g. claude-sonnet-5, gpt-5.5"}>
          <Input
            value={form.model}
            onChange={(e) => set({ model: e.target.value })}
            required={!isJev}
          />
        </Field>
        {!isJev && (
          <Field
            label="Base URL"
            hint={
              needsBaseUrl
                ? "Required: your OpenAI-compatible server, e.g. https://llm.example.com/v1"
                : "Optional: leave blank for the provider's API."
            }
          >
            <Input
              value={form.base_url}
              onChange={(e) => set({ base_url: e.target.value })}
              required={needsBaseUrl}
              placeholder="https://"
            />
          </Field>
        )}
        {!isJev && (
          <Field
            label="API key"
            hint={
              editing?.has_api_key
                ? "A key is stored. Leave blank to keep it."
                : "Encrypted at rest and never shown again."
            }
          >
            <Input
              type="password"
              autoComplete="off"
              value={form.api_key}
              disabled={form.clear_key}
              onChange={(e) => set({ api_key: e.target.value })}
            />
          </Field>
        )}
        {!isJev && editing?.has_api_key && (
          // Its own label: inside the API key Field's label, a click would focus that input.
          <label className="flex items-center gap-2 self-start text-sm sm:col-start-1">
            <input
              type="checkbox"
              checked={form.clear_key}
              onChange={(e) => set({ clear_key: e.target.checked, api_key: "" })}
            />
            Remove the stored key
          </label>
        )}
        {!isJev && (
          <div className="grid grid-cols-2 gap-4">
            <Field label="Max tokens" hint="Default 16000">
              <Input
                type="number"
                min={1}
                value={form.max_tokens}
                onChange={(e) => set({ max_tokens: e.target.value })}
              />
            </Field>
            <Field label="Temperature" hint="Blank = model default">
              <Input
                type="number"
                step="0.1"
                min={0}
                max={2}
                value={form.temperature}
                onChange={(e) => set({ temperature: e.target.value })}
              />
            </Field>
          </div>
        )}
      </div>
      <ErrorText error={save.error} />
      <div className="flex gap-2">
        <Button type="submit" disabled={save.isPending}>
          {save.isPending ? "Saving..." : editing ? "Save changes" : "Add config"}
        </Button>
        <Button type="button" variant="outline" onClick={onDone}>
          Cancel
        </Button>
      </div>
    </form>
  );
}

export function ModelsTab() {
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState<LLMConfig | "new" | null>(null);
  const { data: configs, isLoading, isError } = useQuery({
    queryKey: ["llm-configs"],
    queryFn: () => api.get<LLMConfig[]>("/sre/llm-configs"),
  });
  const remove = useMutation({
    mutationFn: (id: number) => api.delete(`/sre/llm-configs/${id}`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["llm-configs"] }),
  });

  return (
    <Card>
      <CardHeader className="flex flex-row items-start justify-between space-y-0">
        <div className="space-y-1.5">
          <CardTitle>Models</CardTitle>
          <CardDescription>
            Your LLM configs. They're yours: projects you edit can use them, and their keys are
            never shown to anyone.
          </CardDescription>
        </div>
        {editing === null && (
          <Button onClick={() => setEditing("new")}>
            <Plus /> Add config
          </Button>
        )}
      </CardHeader>
      <CardContent className="space-y-4">
        {editing !== null && (
          <ConfigForm
            key={editing === "new" ? "new" : editing.id}
            editing={editing === "new" ? null : editing}
            onDone={() => setEditing(null)}
          />
        )}
        {isLoading && <p className="text-sm text-muted-foreground">Loading...</p>}
        {isError && <p className="text-sm text-destructive">Could not load configs.</p>}
        {configs && configs.length === 0 && editing === null && (
          <p className="text-sm text-muted-foreground">No configs yet. Add one to run the agent.</p>
        )}
        {configs && configs.length > 0 && (
          <div className="rounded-md border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Name</TableHead>
                  <TableHead>Provider</TableHead>
                  <TableHead>Model</TableHead>
                  <TableHead>Base URL</TableHead>
                  <TableHead>API key</TableHead>
                  <TableHead />
                </TableRow>
              </TableHeader>
              <TableBody>
                {configs.map((config) => (
                  <TableRow key={config.id}>
                    <TableCell className="font-medium">{config.name}</TableCell>
                    <TableCell>{providerLabels[config.provider]}</TableCell>
                    <TableCell className="font-mono text-xs">{config.model || "—"}</TableCell>
                    <TableCell className="max-w-[14rem] truncate font-mono text-xs">
                      {config.base_url || "default"}
                    </TableCell>
                    <TableCell>
                      <Badge variant={config.has_api_key ? "secondary" : "outline"}>
                        {config.has_api_key ? "set" : "none"}
                      </Badge>
                    </TableCell>
                    <TableCell className="whitespace-nowrap text-right">
                      <Button
                        variant="ghost"
                        size="icon"
                        aria-label={`Edit ${config.name}`}
                        onClick={() => setEditing(config)}
                      >
                        <Pencil />
                      </Button>
                      <Button
                        variant="ghost"
                        size="icon"
                        aria-label={`Delete ${config.name}`}
                        onClick={() => {
                          if (window.confirm(`Delete "${config.name}"? Projects using it lose it.`)) {
                            remove.mutate(config.id);
                          }
                        }}
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
