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
      <h1 className="mb-4 text-2xl font-semibold">Items</h1>
      {isLoading && <p>Loading...</p>}
      {isError && <p className="text-red-600">Could not load items.</p>}
      {data && data.items.length === 0 && <p className="text-gray-600">No items yet.</p>}
      {data && data.items.length > 0 && (
        <ul className="flex flex-col gap-2">
          {data.items.map((item) => (
            <li key={item.id} className="rounded border bg-white p-3">
              <div className="font-medium">{item.title}</div>
              {item.description && <div className="text-sm text-gray-600">{item.description}</div>}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
