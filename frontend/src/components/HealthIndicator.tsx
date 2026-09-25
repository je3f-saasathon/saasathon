import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { HealthResponse } from "../api/types";

export function HealthIndicator() {
  const { data, isError, isLoading } = useQuery({
    queryKey: ["health"],
    queryFn: () => api.get<HealthResponse>("/health", { skipAuth: true }),
    retry: false,
    refetchInterval: 30_000,
  });

  let label = "checking...";
  let color = "bg-muted-foreground";

  if (!isLoading) {
    if (isError || data?.status !== "ok") {
      label = "unreachable";
      color = "bg-destructive";
    } else {
      label = `ok (v${data.version})`;
      color = "bg-emerald-500 shadow-[0_0_8px_theme(colors.emerald.400)]";
    }
  }

  return (
    <div className="flex items-center gap-2 text-sm text-muted-foreground">
      <span className={`h-2 w-2 rounded-full ${color}`} />
      <span>API: {label}</span>
    </div>
  );
}
