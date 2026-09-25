import { useMemo, useState } from "react";
import { Activity, AlertCircle, Clock, Gauge } from "lucide-react";

import { DataTable } from "@/components/data-table";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { columns } from "./columns";
import { type LogLevel, mockLogs, mockStats } from "./mock-logs";

const stats = [
  {
    title: "Active Errors",
    value: mockStats.activeErrors.toString(),
    hint: "Open or investigating",
    icon: AlertCircle,
  },
  {
    title: "Requests/min",
    value: mockStats.requestsPerMin.toLocaleString(),
    hint: "Last 5 minutes",
    icon: Activity,
  },
  {
    title: "Avg Latency",
    value: `${mockStats.avgLatencyMs} ms`,
    hint: "p50 across all endpoints",
    icon: Gauge,
  },
  {
    title: "Uptime",
    value: `${mockStats.uptimePct}%`,
    hint: "Last 30 days",
    icon: Clock,
  },
];

type LevelFilter = "all" | LogLevel;
const levelFilters: LevelFilter[] = ["all", "error", "warn", "info"];

export function DashboardPage() {
  const [level, setLevel] = useState<LevelFilter>("all");

  const logs = useMemo(
    () => (level === "all" ? mockLogs : mockLogs.filter((log) => log.level === level)),
    [level],
  );

  return (
    <div className="mx-auto max-w-7xl space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Debug dashboard</h1>
        <p className="text-sm text-muted-foreground">Mock data — not connected to a backend yet.</p>
      </div>

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {stats.map(({ title, value, hint, icon: Icon }) => (
          <Card key={title}>
            <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
              <CardTitle className="text-sm font-medium">{title}</CardTitle>
              <Icon className="h-4 w-4 text-muted-foreground" />
            </CardHeader>
            <CardContent>
              <div className="text-2xl font-bold">{value}</div>
              <p className="text-xs text-muted-foreground">{hint}</p>
            </CardContent>
          </Card>
        ))}
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Logs</CardTitle>
        </CardHeader>
        <CardContent>
          <DataTable
            columns={columns}
            data={logs}
            filterColumn="message"
            filterPlaceholder="Search messages..."
            toolbar={
              <Tabs value={level} onValueChange={(value) => setLevel(value as LevelFilter)}>
                <TabsList>
                  {levelFilters.map((l) => (
                    <TabsTrigger key={l} value={l} className="capitalize">
                      {l}
                    </TabsTrigger>
                  ))}
                </TabsList>
              </Tabs>
            }
          />
        </CardContent>
      </Card>
    </div>
  );
}
