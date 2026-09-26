import type { ModelTokens } from "@/api/types";
import { cn } from "@/lib/utils";

/** A model's name, falling back to its provider when the call didn't report one. */
export function modelName(m: Pick<ModelTokens, "provider" | "model">): string {
  return m.model || m.provider || "unknown";
}

/** Sums per-model token lists (e.g. from several runs) into one, biggest first. */
export function mergeByModel(lists: ModelTokens[][]): ModelTokens[] {
  const merged = new Map<string, ModelTokens>();
  for (const m of lists.flat()) {
    const key = `${m.provider}\u0000${m.model}`;
    const entry = merged.get(key);
    if (!entry) {
      merged.set(key, { ...m });
      continue;
    }
    entry.calls += m.calls;
    entry.input_tokens += m.input_tokens;
    entry.cached_input_tokens += m.cached_input_tokens;
    entry.output_tokens += m.output_tokens;
    entry.total_tokens += m.total_tokens;
  }
  return [...merged.values()].sort((a, b) => b.total_tokens - a.total_tokens);
}

/** One line per model with its own token count: models are priced differently, so a
 * single summed number hides what the tokens cost. */
export function ModelTokensList({
  models,
  className,
}: {
  models: ModelTokens[];
  className?: string;
}) {
  if (models.length === 0) return null;
  return (
    <ul className={cn("space-y-0.5 font-mono text-xs", className)} data-testid="model-tokens">
      {models.map((m) => (
        <li key={`${m.provider}-${m.model}`} className="flex justify-between gap-3 whitespace-nowrap">
          <span className="truncate">{modelName(m)}</span>
          <span>{m.total_tokens.toLocaleString()}</span>
        </li>
      ))}
    </ul>
  );
}
