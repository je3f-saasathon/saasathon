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
  default_llm_config_id: 1, generate_tests: true, platform_preset: "openai_jev", platform_tokens_this_month: 0, platform_tokens_by_model_this_month: [], created_at: "2026-09-25T00:00:00Z",
  github_verified: false, service_names: [], uptrace_managed: false, uptrace_status: "",
  uptrace_error: "", uptrace_project_id: null, uptrace_dsn: "", uptrace_shared_with: [],
};

const managedProject = {
  ...project, id: 4, name: "sales-order", service_names: ["sales-order"], uptrace_managed: true,
  uptrace_status: "ready", uptrace_project_id: 12, uptrace_source_id: "uptrace.buggly.dev/12",
  uptrace_dsn: "https://ingest-token@uptrace.buggly.dev?grpc=4317",
  uptrace_shared_with: [{ id: 5, name: "sales-cart" }],
};

const routes: Record<string, unknown> = {
  "GET /api/sre/llm-configs": configs,
  "POST /api/sre/llm-configs": { ...configs[0], id: 3, name: "claude" },
  "PATCH /api/sre/llm-configs/1": { ...configs[0], has_api_key: false },
  "GET /api/sre/projects": [project],
  "GET /api/sre/projects/3/step-overrides": [],
  "GET /api/sre/platform": {
    available: true, triage_model: "jev", strong_model: "gpt-5.5", monthly_token_cap: 2000000,
    default_preset: "openai_jev",
    presets: [
      { key: "openai_jev", label: "OpenAI + Jev", triage_model: "jev", strong_model: "gpt-5.5" },
      { key: "openai", label: "OpenAI", triage_model: "gpt-5.4-mini", strong_model: "gpt-5.5" },
      { key: "gpt_5_4", label: "GPT-5.4 only", triage_model: "gpt-5.4", strong_model: "gpt-5.4" },
      { key: "gpt_5_5", label: "GPT-5.5 only", triage_model: "gpt-5.5", strong_model: "gpt-5.5" },
    ],
  },
  "PATCH /api/sre/projects/3": {},
  "PATCH /api/sre/projects/2": { ...project, generate_tests: false },
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
    routes["GET /api/sre/projects"] = [project];
    delete routes["GET /api/sre/uptrace/managed"];
    delete routes["POST /api/sre/projects/2/uptrace/setup"];
  });

  it("lists model configs without ever rendering a key, and creates one", async () => {
    renderAt("/settings?tab=models");
    expect(await screen.findByText("openai-strong")).toBeInTheDocument();
    expect(screen.getByText("set")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Add config/ }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "claude" } });
    fireEvent.change(screen.getByLabelText(/^Model\s*e\.g\./), { target: { value: "claude-sonnet-5" } });
    fireEvent.change(screen.getByLabelText(/^API key/), { target: { value: "sk-secret" } });
    const addButtons = screen.getAllByRole("button", { name: "Add config" });
    fireEvent.click(addButtons[addButtons.length - 1]);

    await vi.waitFor(() =>
      expect(fetchMock.mock.calls.some(([, init]) => init?.method === "POST")).toBe(true),
    );
    const [, init] = fetchMock.mock.calls.find(([, i]) => i?.method === "POST")!;
    expect(JSON.parse(init.body)).toMatchObject({
      name: "claude", provider: "anthropic", model: "claude-sonnet-5", api_key: "sk-secret",
    });
  });

  it("clears a stored key only when asked", async () => {
    renderAt("/settings?tab=models");
    fireEvent.click(await screen.findByRole("button", { name: "Edit openai-strong" }));
    fireEvent.click(screen.getByLabelText("Remove the stored key"));
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    await vi.waitFor(() =>
      expect(fetchMock.mock.calls.some(([, init]) => init?.method === "PATCH")).toBe(true),
    );
    const [, init] = fetchMock.mock.calls.find(([, i]) => i?.method === "PATCH")!;
    expect(JSON.parse(init.body).api_key).toBe("");
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

  it("turning off test generation sends only that change", async () => {
    renderAt("/settings?tab=projects");
    fireEvent.click(await screen.findByText("django-buggy-app"));
    const toggle = await screen.findByRole("checkbox", { name: /Generate tests/ });
    expect(toggle).toBeChecked();
    fireEvent.click(toggle);
    fireEvent.click(screen.getByRole("button", { name: "Save project" }));
    await vi.waitFor(() =>
      expect(fetchMock.mock.calls.some(([, init]) => init?.method === "PATCH")).toBe(true),
    );
    const [, init] = fetchMock.mock.calls.find(([, i]) => i?.method === "PATCH")!;
    expect(JSON.parse(init.body)).toEqual({ generate_tests: false });
  });

  it("offers the company default for a project without its own model, with its usage", async () => {
    routes["GET /api/sre/projects"] = [
      project,
      { ...project, id: 3, name: "bare-project", default_llm_config_id: null, platform_tokens_this_month: 500000,
        platform_tokens_by_model_this_month: [
          { provider: "openai", model: "gpt-5.5", calls: 3, input_tokens: 400000, cached_input_tokens: 0, output_tokens: 50000, total_tokens: 450000 },
          { provider: "jev_cloudflare", model: "jev", calls: 9, input_tokens: 45000, cached_input_tokens: 0, output_tokens: 5000, total_tokens: 50000 },
        ] },
    ];
    renderAt("/settings?tab=projects");
    fireEvent.click(await screen.findByText("bare-project"));
    const select = await screen.findByLabelText("Default model");
    await vi.waitFor(() => expect(select).toHaveValue("company:openai_jev"));
    for (const name of ["OpenAI + Jev (Jev + gpt-5.5)", "OpenAI (gpt-5.4-mini + gpt-5.5)",
                        "GPT-5.4 only", "GPT-5.5 only"]) {
      expect(screen.getByRole("option", { name })).toBeInTheDocument();
    }
    fireEvent.change(select, { target: { value: "company:gpt_5_4" } });
    await vi.waitFor(() =>
      expect(fetchMock.mock.calls.some(([u, i]) => i?.method === "PATCH" && String(u).endsWith("/projects/3"))).toBe(true),
    );
    const [, patch] = fetchMock.mock.calls.find(([u, i]) => i?.method === "PATCH" && String(u).endsWith("/projects/3"))!;
    expect(JSON.parse(patch.body)).toEqual({ default_llm_config_id: null, platform_preset: "gpt_5_4" });
    expect(await screen.findByTestId("company-usage")).toHaveTextContent("500,000 / 2,000,000 tokens");
    expect(screen.getByTestId("model-tokens")).toHaveTextContent("gpt-5.5450,000jev50,000");
    expect(screen.queryByText(/No default model/)).not.toBeInTheDocument();
    routes["GET /api/sre/projects"] = [project];
  });

  it("shows a managed project's DSN and how to send telemetry, with no Uptrace setup steps", async () => {
    routes["GET /api/sre/projects"] = [managedProject];
    routes["GET /api/sre/projects/4/step-overrides"] = [];
    routes["GET /api/sre/uptrace/managed"] = { enabled: true, url: "https://uptrace.buggly.dev" };
    renderAt("/settings?tab=projects");
    fireEvent.click(await screen.findByText("sales-order"));

    expect(await screen.findByText("Ready")).toBeInTheDocument();
    expect(screen.getByDisplayValue("https://ingest-token@uptrace.buggly.dev?grpc=4317")).toBeInTheDocument();
    expect(await screen.findByText("https://uptrace.buggly.dev/v1/traces")).toBeInTheDocument();
    expect(screen.getByText(/Shares its Uptrace project with sales-cart/)).toBeInTheDocument();
    // The platform owns the pin and the credential.
    expect(screen.queryByLabelText(/^Uptrace project/)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/^Uptrace credential/)).not.toBeInTheDocument();
    expect(screen.getByText("Webhook secret")).toBeInTheDocument();
  });

  it("lets an owner hand a hand-wired project's Uptrace to the platform", async () => {
    routes["GET /api/sre/uptrace/managed"] = { enabled: true, url: "https://uptrace.buggly.dev" };
    routes["POST /api/sre/projects/2/uptrace/setup"] = { ...project, uptrace_managed: true, uptrace_status: "provisioning" };
    renderAt("/settings?tab=projects");
    fireEvent.click(await screen.findByText("django-buggy-app"));
    fireEvent.click(await screen.findByRole("button", { name: "Let the platform manage Uptrace" }));
    await vi.waitFor(() =>
      expect(fetchMock.mock.calls.some(([url, init]) =>
        init?.method === "POST" && String(url).endsWith("/uptrace/setup"))).toBe(true),
    );
    const [, init] = fetchMock.mock.calls.find(([url]) => String(url).endsWith("/uptrace/setup"))!;
    expect(JSON.parse(init.body)).toEqual({ share_with_project_id: null });
  });

  it("lets an owner delete a project after confirming, showing API errors", async () => {
    let deleteStatus = 503;
    const base = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (init?.method !== "DELETE") return base(url, init);
      if (deleteStatus !== 204) {
        return {
          ok: false, status: deleteStatus, statusText: "Service Unavailable",
          json: async () => ({ detail: "Could not cancel this project's running work in Temporal" }),
        };
      }
      routes["GET /api/sre/projects"] = [];
      return { ok: true, status: 204, json: async () => ({}) };
    });
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    const deletes = () => fetchMock.mock.calls.filter(([, i]) => i?.method === "DELETE");

    renderAt("/settings?tab=projects");
    fireEvent.click(await screen.findByText("django-buggy-app"));
    const button = await screen.findByRole("button", { name: "Delete project" });
    fireEvent.click(button);
    expect(confirm.mock.calls[0][0]).toMatch(/cancelled[\s\S]*left open/);
    expect(deletes()).toHaveLength(0);

    confirm.mockReturnValue(true);
    fireEvent.click(button);
    expect(await screen.findByText(/Could not cancel this project's running work/)).toBeInTheDocument();
    expect(String(deletes()[0][0])).toMatch(/\/api\/sre\/projects\/2$/);

    deleteStatus = 204;
    fireEvent.click(screen.getByRole("button", { name: "Delete project" }));
    expect(await screen.findByText("No projects yet.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Delete project" })).not.toBeInTheDocument();
    confirm.mockRestore();
  });

  it("hides delete from a project admin who isn't the owner", async () => {
    routes["GET /api/sre/projects"] = [{ ...project, role: "admin" }];
    renderAt("/settings?tab=projects");
    fireEvent.click(await screen.findByText("django-buggy-app"));
    expect(await screen.findByText("Models for this project")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Delete project" })).not.toBeInTheDocument();
  });

  it("creates a project on the user's own Uptrace even when managed Uptrace is on", async () => {
    routes["GET /api/sre/uptrace/managed"] = { enabled: true, url: "https://uptrace.buggly.dev" };
    routes["POST /api/sre/projects"] = { ...project, id: 7, name: "shop", webhook_secret: "s", webhook_url: "u" };
    renderAt("/settings?tab=projects");
    fireEvent.click(await screen.findByRole("button", { name: /New project/ }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "shop" } });
    await screen.findByRole("option", { name: /je3f-saasathon/ });
    fireEvent.change(screen.getByLabelText("GitHub installation"), { target: { value: "164850677" } });
    const repo = await screen.findByRole("option", { name: "je3f-saasathon/django-buggy-app" });
    fireEvent.change(repo.closest("select")!, { target: { value: "je3f-saasathon/django-buggy-app" } });

    // Managed by default: no pin field until the user picks their own Uptrace.
    expect(screen.queryByLabelText(/^Uptrace project/)).not.toBeInTheDocument();
    fireEvent.change(await screen.findByLabelText(/^Monitoring/), { target: { value: "own" } });
    expect(screen.getByLabelText(/^Uptrace project/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Create/ }));

    await vi.waitFor(() =>
      expect(fetchMock.mock.calls.some(([url, init]) =>
        init?.method === "POST" && new URL(String(url)).pathname === "/api/sre/projects")).toBe(true),
    );
    const [, init] = fetchMock.mock.calls.find(([url, i]) =>
      i?.method === "POST" && new URL(String(url)).pathname === "/api/sre/projects")!;
    const body = JSON.parse(init.body);
    expect(body.uptrace_managed).toBe(false);
    expect(body.uptrace_share_with_project_id).toBeNull();
    delete routes["POST /api/sre/projects"];
  });

  it("saves service names as a list, only when they change", async () => {
    renderAt("/settings?tab=projects");
    fireEvent.click(await screen.findByText("django-buggy-app"));
    fireEvent.change(await screen.findByLabelText(/^Service names/), {
      target: { value: "django-buggy-app, worker ," },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save project" }));
    await vi.waitFor(() =>
      expect(fetchMock.mock.calls.some(([, init]) => init?.method === "PATCH")).toBe(true),
    );
    const [, init] = fetchMock.mock.calls.find(([, i]) => i?.method === "PATCH")!;
    expect(JSON.parse(init.body)).toEqual({ service_names: ["django-buggy-app", "worker"] });
  });

  it("shows the connected banner and installations on the GitHub tab", async () => {
    renderAt("/settings?tab=github&github=connected&count=1");
    expect(screen.getByText(/GitHub connected: 1 installation available/)).toBeInTheDocument();
    expect(await screen.findByText("je3f-saasathon")).toBeInTheDocument();
    // Already connected: the buttons read as adding to or refreshing the connection.
    expect(await screen.findByRole("button", { name: /Add another account/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Refresh from GitHub" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Connect GitHub/ })).not.toBeInTheDocument();
  });

  it("says how many projects connecting created", () => {
    renderAt("/settings?tab=github&github=connected&count=1&projects=2");
    expect(screen.getByText(/Created 2 projects, one per repo/)).toBeInTheDocument();
  });

  it("explains a connect error", () => {
    renderAt("/settings?tab=github&github_error=state_expired");
    expect(screen.getByText(/connect link expired/)).toBeInTheDocument();
  });
});
