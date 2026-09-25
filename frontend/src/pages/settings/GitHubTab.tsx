import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plug, Unlink } from "lucide-react";
import { useSearchParams } from "react-router-dom";

import { api } from "@/api/client";
import type { GitHubConnect, GitHubInstallation, GitHubStatus } from "@/api/types";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { ErrorText } from "./form";

const errorMessages: Record<string, string> = {
  invalid_state: "The connect link was invalid. Please try again.",
  state_expired: "The connect link expired. Please try again.",
  authorization_missing:
    "GitHub didn't send an authorization. Use “Re-link existing installation”, or check the App has “Request user authorization during installation” ticked.",
  token_exchange_failed: "GitHub rejected the authorization. Please try again.",
  github_api_failed: "Couldn't read your installations from GitHub. Please try again.",
  not_configured: "The GitHub App isn't configured on this server.",
};

function Banner() {
  const [params] = useSearchParams();
  const error = params.get("github_error");
  if (error) {
    return (
      <p className="rounded-md border border-destructive/50 p-3 text-sm text-destructive">
        {errorMessages[error] ?? `GitHub connect failed (${error}).`}
      </p>
    );
  }
  if (params.get("github") === "connected") {
    const count = Number(params.get("count") ?? 0);
    return (
      <p className="rounded-md border border-emerald-500/50 p-3 text-sm text-emerald-700 dark:text-emerald-300">
        GitHub connected: {count} installation{count === 1 ? "" : "s"} available.
      </p>
    );
  }
  return null;
}

export function GitHubTab() {
  const queryClient = useQueryClient();
  const status = useQuery({
    queryKey: ["github-status"],
    queryFn: () => api.get<GitHubStatus>("/sre/github/status"),
  });
  const installations = useQuery({
    queryKey: ["github-installations"],
    queryFn: () => api.get<GitHubInstallation[]>("/sre/github/installations"),
  });
  const connect = useMutation({
    mutationFn: () => api.post<GitHubConnect>("/sre/github/connect"),
  });
  const unlink = useMutation({
    mutationFn: (id: number) => api.delete(`/sre/github/installations/${id}`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["github-installations"] }),
  });

  async function go(which: keyof GitHubConnect) {
    const urls = await connect.mutateAsync();
    window.location.assign(urls[which]);
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>GitHub</CardTitle>
        <CardDescription>
          The agent opens pull requests through our GitHub App. Connect GitHub to prove which
          installations you can use; projects can only point at those.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <Banner />
        {status.data && !status.data.configured && (
          <p className="text-sm text-muted-foreground">
            The GitHub App isn't configured on this server yet. Set <code>GITHUB_APP_SLUG</code>,{" "}
            <code>GITHUB_APP_CLIENT_ID</code> and <code>GITHUB_APP_CLIENT_SECRET</code> (see{" "}
            <code>docs/AUTH.md</code>).
          </p>
        )}
        {status.data?.configured && (
          <div className="flex flex-wrap gap-2">
            <Button onClick={() => go("install_url")} disabled={connect.isPending}>
              <Plug /> Connect GitHub
            </Button>
            <Button
              variant="outline"
              onClick={() => go("authorize_url")}
              disabled={connect.isPending}
            >
              Re-link existing installation
            </Button>
          </div>
        )}
        <ErrorText error={connect.error ?? unlink.error} />

        {installations.data && installations.data.length === 0 && (
          <p className="text-sm text-muted-foreground">No connected installations.</p>
        )}
        {installations.data && installations.data.length > 0 && (
          <div className="rounded-md border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Account</TableHead>
                  <TableHead>Type</TableHead>
                  <TableHead>Installation id</TableHead>
                  <TableHead />
                </TableRow>
              </TableHeader>
              <TableBody>
                {installations.data.map((inst) => (
                  <TableRow key={inst.id}>
                    <TableCell className="font-medium">{inst.account_login}</TableCell>
                    <TableCell>{inst.account_type || "—"}</TableCell>
                    <TableCell className="font-mono text-xs">{inst.installation_id}</TableCell>
                    <TableCell className="text-right">
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => unlink.mutate(inst.id)}
                        title="Removes it from your account here; nothing changes on GitHub"
                      >
                        <Unlink /> Unlink
                      </Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
