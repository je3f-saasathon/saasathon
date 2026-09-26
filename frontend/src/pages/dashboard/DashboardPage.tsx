import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { AlertCircle, Coins, GitPullRequest, Siren } from "lucide-react";

import { api } from "@/api/client";
import type { IncidentRunList } from "@/api/types";
import { DataTable } from "@/components/data-table";
import { mergeByModel, ModelTokensList } from "@/components/ModelTokens";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent } from "@/components/ui/dialog";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { columns } from "./columns";
import { ConnectGitHubCard } from "./ConnectGitHubCard";
import { IncidentDetail } from "./IncidentDetail";

const PAGE_SIZE = 25;

const statusFilters = ["all", "running", "awaiting_approval", "succeeded", "rejected", "failed"] as const;
type StatusFilter = (typeof statusFilters)[number];

export function DashboardPage() {
  const [status, setStatus] = useState<StatusFilter>("all");
  const [selectedId, setSelectedId] = useState<number | null>(null);

  const { data, isLoading, isError } = useQuery({
    queryKey: ["incident-runs"],
    queryFn: () => api.get<IncidentRunList>(`/sre/incident-runs?page_size=${PAGE_SIZE}`),
    // Running incidents change state in the background.
    refetchInterval: 15000,
  });

  const runs = useMemo(() => data?.runs ?? [], [data]);
  const visible = useMemo(
    () => (status === "all" ? runs : runs.filter((run) => run.status === status)),
    [runs, status],
  );
  const selected = runs.find((run) => run.id === selectedId);

  const hint = `Last ${runs.length} incidents`;
  const stats = [
    { title: "Incidents", value: runs.length, icon: Siren },
    { title: "PRs opened", value: runs.filter((run) => run.pr_url).length, icon: GitPullRequest },
    {
      title: "Awaiting approval",
      value: runs.filter((run) => run.status === "awaiting_approval").length,
      icon: AlertCircle,
    },
    {
      title: "Tokens used",
      value: runs.reduce((sum, run) => sum + run.usage.total_tokens, 0),
      icon: Coins,
      byModel: mergeByModel(runs.map((run) => run.usage.by_model)),
    },
  ];

  return (
    <div className="mx-auto max-w-7xl space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Incidents</h1>
        <p className="text-sm text-muted-foreground">
          Your latest SRE incidents across all projects.
        </p>
      </div>

      <ConnectGitHubCard />

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {stats.map(({ title, value, icon: Icon, byModel }) => (
          <Card key={title}>
            <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
              <CardTitle className="text-sm font-medium">{title}</CardTitle>
              <Icon className="h-4 w-4 text-muted-foreground" />
            </CardHeader>
            <CardContent>
              <div className="text-2xl font-bold">{data ? value.toLocaleString() : "—"}</div>
              <p className="text-xs text-muted-foreground">{hint}</p>
              {byModel && (
                <ModelTokensList models={byModel} className="mt-2 text-muted-foreground" />
              )}
            </CardContent>
          </Card>
        ))}
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Incidents</CardTitle>
        </CardHeader>
        <CardContent>
          {isLoading && <p className="text-sm text-muted-foreground">Loading incidents...</p>}
          {isError && <p className="text-sm text-destructive">Could not load incidents.</p>}
          {data && (
            <DataTable
              columns={columns}
              data={visible}
              filterColumn="incident"
              filterPlaceholder="Search incidents..."
              getRowId={(run) => String(run.id)}
              selectedRowId={selectedId != null ? String(selectedId) : undefined}
              onRowClick={(run) => setSelectedId(run.id === selectedId ? null : run.id)}
              toolbar={
                <Tabs value={status} onValueChange={(value) => setStatus(value as StatusFilter)}>
                  <TabsList>
                    {statusFilters.map((s) => (
                      <TabsTrigger key={s} value={s} className="capitalize">
                        {s.replace(/_/g, " ")}
                      </TabsTrigger>
                    ))}
                  </TabsList>
                </Tabs>
              }
            />
          )}
        </CardContent>
      </Card>

      <Dialog open={!!selected} onOpenChange={(open) => !open && setSelectedId(null)}>
        <DialogContent
          aria-describedby={undefined}
          className="w-[calc(100%-2rem)] max-w-5xl p-0"
        >
          {selected && <IncidentDetail run={selected} onClose={() => setSelectedId(null)} />}
        </DialogContent>
      </Dialog>
    </div>
  );
}
