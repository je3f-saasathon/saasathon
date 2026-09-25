import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SettingsPage } from "./SettingsPage";

const configs = [
  { id: 1, name: "openai-strong", provider: "openai", model: "gpt-5.5", base_url: "", has_api_key: true, extra_config: {}, created_at: "2026-09-25T00:00:00Z" },
  { id: 2, name: "jev", provider: "jev_cloudflare", model: "", base_url: "", has_api_key: false, extra_config: {}, created_at: "2026-09-25T00:00:00Z" },
];

const project = {
  id: 2, name: "django-buggy-app", role: "owner", github_installation_id: "164850677",
  github_repo_owner: "je3f-saasathon", github_repo_name: "django-buggy-app",
  github_default_branch: "main", uptrace_source_id: "", default_execution_mode: "draft_only",
  default_llm_config_id: 1, created_at: "2026-09-25T00:00:00Z", github_verified: false,
};

const routes: Record<string, unknown> = {
  "GET /api/sre/llm-configs": configs,
  "POST /api/sre/llm-configs": { ...configs[0], id: 3, name: "claude" },
  "GET /api/sre/projects": [project],
  "GET /api/sre/projects/2/step-overrides": [],
  "GET /api/sre/github/status": { configured: true, app_slug: "sre-app-local" },
  "GET /api/sre/github/installations": [{ id: 9, installation_id: "164850677", account_login: "je3f-saasathon", account_type: "Organization" }],
  "GET /api/sre/github/installations/9/repos": [{ owner: "je3f-saasathon", name: "django-buggy-app", default_branch: "main", private: false }],
};

let fetchMock: ReturnType<typeof vi.fn>;

function renderAt(url: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[url]}>
        <SettingsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("SettingsPage", () => {
  beforeEach(() => {
    fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      const key = `${init?.method ?? "GET"} ${new URL(url).pathname}`;
      if (!(key in routes)) return { ok: false, status: 404, statusText: key, json: async () => ({}) };
      return { ok: true, status: 200, json: async () => routes[key] };
    });
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("lists model configs without ever rendering a key, and creates one", async () => {
    renderAt("/settings?tab=models");
    expect(await screen.findByText("openai-strong")).toBeInTheDocument();
    expect(screen.getByText("set")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Add config/ }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "claude" } });
    fireEvent.change(screen.getByLabelText(/^Model\s*e\.g\./), { target: { value: "claude-sonnet-5" } });
    fireEvent.change(screen.getByLabelText(/^API key/), { target: { value: "sk-secret" } });
    fireEvent.click(screen.getAllByRole("button", { name: "Add config" }).at(-1)!);

    await vi.waitFor(() =>
      expect(fetchMock.mock.calls.some(([, init]) => init?.method === "POST")).toBe(true),
    );
    const [, init] = fetchMock.mock.calls.find(([, i]) => i?.method === "POST")!;
    expect(JSON.parse(init.body)).toMatchObject({
      name: "claude", provider: "anthropic", model: "claude-sonnet-5", api_key: "sk-secret",
    });
  });

  it("shows project models with Jev disabled for steps it can't run", async () => {
    renderAt("/settings?tab=projects");
    fireEvent.click(await screen.findByText("django-buggy-app"));
    expect(await screen.findByText("Unverified")).toBeInTheDocument();

    const execution = await screen.findByLabelText("Model for Playbook execution (agent)");
    expect(within(execution).getByRole("option", { name: /jev/ })).toBeDisabled();
    const triage = screen.getByLabelText("Model for Anomaly double-check");
    expect(within(triage).getByRole("option", { name: /jev/ })).not.toBeDisabled();
  });

  it("shows the connected banner and installations on the GitHub tab", async () => {
    renderAt("/settings?tab=github&github=connected&count=1");
    expect(screen.getByText(/GitHub connected: 1 installation available/)).toBeInTheDocument();
    expect(await screen.findByText("je3f-saasathon")).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: /Connect GitHub/ })).toBeInTheDocument();
  });

  it("explains a connect error", () => {
    renderAt("/settings?tab=github&github_error=state_expired");
    expect(screen.getByText(/connect link expired/)).toBeInTheDocument();
  });
});
