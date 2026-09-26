import { useMutation, useQuery } from "@tanstack/react-query";
import { GitBranch, Plug } from "lucide-react";

import { api } from "@/api/client";
import type { GitHubConnect, GitHubInstallation, GitHubStatus } from "@/api/types";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { ErrorText } from "../settings/form";

// Nudges users with no GitHub installation to connect one; hidden once they have.
// The connect callback lands on Settings → GitHub, which shows the result.
export function ConnectGitHubCard() {
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

  if (!status.data?.configured || !installations.data || installations.data.length > 0) {
    return null;
  }

  async function go() {
    const urls = await connect.mutateAsync();
    window.location.assign(urls.install_url);
  }

  return (
    <Card>
      <CardHeader className="flex flex-row items-center gap-4 space-y-0">
        <GitBranch className="h-8 w-8 shrink-0 text-muted-foreground" />
        <div className="flex-1 space-y-1">
          <CardTitle>Connect GitHub</CardTitle>
          <CardDescription>
            The agent opens pull requests through our GitHub App. Connect GitHub so your projects
            can point at your repositories.
          </CardDescription>
        </div>
        <Button onClick={go} disabled={connect.isPending}>
          <Plug /> Connect GitHub
        </Button>
      </CardHeader>
      {connect.error && (
        <CardContent>
          <ErrorText error={connect.error} />
        </CardContent>
      )}
    </Card>
  );
}
