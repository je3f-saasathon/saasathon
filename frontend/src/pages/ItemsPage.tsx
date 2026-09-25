import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { ItemListResponse } from "../api/types";

export function ItemsPage() {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["items"],
    queryFn: () => api.get<ItemListResponse>("/items"),
  });

  return (
    <div>
      <h1 className="mb-4 text-2xl font-semibold tracking-tight">Items</h1>
      {isLoading && <p className="text-muted-foreground">Loading...</p>}
      {isError && <p className="text-destructive">Could not load items.</p>}
      {data && data.items.length === 0 && <p className="text-muted-foreground">No items yet.</p>}
      {data && data.items.length > 0 && (
        <ul className="flex flex-col gap-2">
          {data.items.map((item) => (
            <li key={item.id} className="rounded-lg border bg-card p-3 text-card-foreground">
              <div className="font-medium">{item.title}</div>
              {item.description && <div className="text-sm text-muted-foreground">{item.description}</div>}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
