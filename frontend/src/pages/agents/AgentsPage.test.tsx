import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AgentsPage } from "./AgentsPage";

const org = { id: 1, name: "acme", is_personal: false, role: "owner", created_at: "2026-09-25T00:00:00Z" };

const agent = {
  id: 4, organization_id: 1, name: "Nightly sweep", kind: "playbook_sweep", trigger: "schedule",
  schedule_cron: "0 3 * * *", branch_pattern: "", project_ids: [], playbook_ids: [],
  execution_mode: "advisory_only", max_findings_per_repo: 3, code_findings_open_prs: false, monthly_token_budget: 500000,
  tokens_this_month: 12000,
  tokens_by_model_this_month: [
    { provider: "openai", model: "gpt-5.5", calls: 1, input_tokens: 7000, cached_input_tokens: 0, output_tokens: 2000, total_tokens: 9000 },
    { provider: "jev_cloudflare", model: "jev", calls: 1, input_tokens: 2000, cached_input_tokens: 0, output_tokens: 1000, total_tokens: 3000 },
  ],
  enabled: true, created_by_id: 1, created_at: "2026-09-25T00:00:00Z",
  updated_at: "2026-09-25T00:00:00Z", last_scan_run_id: 9,
};

const usage = {
  calls: 2, input_tokens: 9000, cached_input_tokens: 0, output_tokens: 3000, total_tokens: 12000,
  platform_tokens: 12000, models: ["gpt-5.5"], by_step: [],
  by_model: [
    { provider: "openai", model: "gpt-5.5", calls: 2, input_tokens: 9000, cached_input_tokens: 0, output_tokens: 3000, total_tokens: 12000, platform_tokens: 12000 },
  ],
};

const scanRun = {
  id: 9, agent_id: 4, trigger: "schedule", trigger_ref: "2026-09-26T03:00:00Z", status: "partial",
  finding_count: 1, usage, error_message: "", started_at: "2026-09-26T03:00:01Z",
  finished_at: "2026-09-26T03:02:31Z",
  repos: [
    {
      project_id: 2, project_name: "api", status: "succeeded", finding_count: 2, incident_run_ids: [31, 32], error: "",
      findings: [
        { incident_run_id: 31, status: "awaiting_approval", pr_url: "https://github.com/acme/api/pull/5", mode_note: "" },
        { incident_run_id: 32, status: "advisory_complete", pr_url: "",
          mode_note: "Diagnosis only: incident #31 is already fixing this in acme/api" },
      ],
    },
    { project_id: 3, project_name: "worker", status: "failed", finding_count: 0, incident_run_ids: [], findings: [], error: "clone failed" },
    { project_id: 4, project_name: "web", status: "skipped", finding_count: 0, incident_run_ids: [], findings: [],
      error: "No new commits on main since scan run 8" },
  ],
};

const incident = {
  id: 31, project_id: 2, project_name: "api", trace_id: "scan-playbook_sweep-api-1a2b",
  uptrace_exception_id: "", temporal_workflow_id: "sre-incident-2-scan-playbook_sweep-1a2b",
  status: "advisory_complete", classification: null, matched_playbook_id: null, created_playbook_id: null,
  playbook_run_id: null, diagnosis_report: "", error_message: "", created_at: "2026-09-26T03:02:00Z",
  updated_at: "2026-09-26T03:05:00Z", playbook: null, pr_url: "", playbook_run_status: null,
  execution_mode: null, generate_tests: null, usage: { ...usage, calls: 0, total_tokens: 0, by_model: [] },
  matched_runbook_id: null, runbook: null, source: "scan", parent_incident_run_id: null, root_cause: {},
  scan_run_id: 9, scan_kind: "playbook_sweep", mode_note: "", covered_by_incident_run_id: null, covered_by_pr_url: "",
  telemetry: {
    exception_type: "null_reference", title: "Order total read before the cart loads",
    message: "cart may be None here", location: "shop/views.py:42", evidence: "total = cart.total",
    evidence_kind: "code", service_name: "api",
  },
};

const project = (id: number, name: string) => ({
  id, name, organization_id: 1, github_repo_owner: "acme", github_repo_name: name,
});

let routes: Record<string, unknown>;
let fetchMock: ReturnType<typeof vi.fn>;

function renderAt(url: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[url]}>
        <AgentsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function sent(method: string) {
  const call = fetchMock.mock.calls.find(([, init]) => init?.method === method);
  return call && { path: new URL(call[0]).pathname, body: call[1].body && JSON.parse(call[1].body) };
}

describe("AgentsPage", () => {
  beforeEach(() => {
    routes = {
      "GET /api/sre/organizations": [org],
      "GET /api/sre/organizations/1/agents": [agent],
      "GET /api/sre/agents/4/scan-runs": { scan_runs: [scanRun], total: 1 },
      "GET /api/sre/incident-runs/31": incident,
      "GET /api/sre/scan-runs/9": scanRun,
      "GET /api/sre/projects": [project(2, "api"), project(3, "worker")],
      "GET /api/sre/projects/2/playbooks": {
        total: 2,
        playbooks: [
          { id: 1, title: "Null checks", origin: "builtin", is_generic: true, organization_id: null, status: "confirmed" },
          { id: 8, title: "Legacy one-off", origin: "agent", is_generic: false, organization_id: 1, status: "confirmed" },
        ],
      },
      "POST /api/sre/organizations/1/agents": { ...agent, id: 5, name: "Release watch" },
      "POST /api/sre/agents/4/run": { ...scanRun, id: 10, status: "running" },
    };
    fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      const key = `${init?.method ?? "GET"} ${new URL(url).pathname}`;
      if (!(key in routes)) return { ok: false, status: 404, statusText: key, json: async () => ({ detail: "Not found" }) };
      return { ok: true, status: 200, json: async () => routes[key] };
    });
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("lists agents with their trigger, budget and latest scan run", async () => {
    renderAt("/agents");
    expect(await screen.findByText("Nightly sweep")).toBeInTheDocument();
    expect(screen.getByText("Playbook sweep")).toBeInTheDocument();
    expect(screen.getByText("Schedule · 0 3 * * *")).toBeInTheDocument();
    expect(screen.getByText(`${(12000).toLocaleString()} / ${(500000).toLocaleString()}`)).toBeInTheDocument();
    // The month's tokens split per model.
    const byModel = screen.getByTestId("model-tokens");
    expect(within(byModel).getByText("gpt-5.5").parentElement).toHaveTextContent(`gpt-5.5${(9000).toLocaleString()}`);
    expect(within(byModel).getByText("jev").parentElement).toHaveTextContent(`jev${(3000).toLocaleString()}`);

    // The latest run is expanded, with each repo's outcome.
    expect(await screen.findByText("some repos failed")).toBeInTheDocument();
    expect(screen.getByText("clone failed")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Incident #31" })).toBeInTheDocument();
    // Each finding's PR, or why it stopped at a diagnosis; a skip isn't an error.
    expect(screen.getByRole("link", { name: /PR #5/ })).toHaveAttribute("href", "https://github.com/acme/api/pull/5");
    expect(screen.getByText("Diagnosis only: incident #31 is already fixing this in acme/api")).toBeInTheDocument();
    expect(screen.getByText("No new commits on main since scan run 8")).not.toHaveClass("text-destructive");
    expect(screen.getByText("clone failed")).toHaveClass("text-destructive");
  });

  it("opens a finding's incident with what the scanner found", async () => {
    renderAt("/agents");
    fireEvent.click(await screen.findByRole("button", { name: "Incident #31" }));
    const dialog = await screen.findByRole("dialog", { name: /Order total read before the cart loads/ });
    expect(within(dialog).getByText("shop/views.py:42")).toBeInTheDocument();
    expect(within(dialog).getByText("From reading the code")).toBeInTheDocument();
    expect(await within(dialog).findByRole("link", { name: "scan run #9" })).toHaveAttribute("href", "/agents?agent=4");
    // Its trace_id is a dedup key, so it isn't shown as a trace.
    expect(within(dialog).queryByText(/trace scan-/)).not.toBeInTheDocument();
  });

  it("says why a finding stopped at a diagnosis, with the PR that covers it", async () => {
    routes["GET /api/sre/incident-runs/31"] = {
      ...incident,
      mode_note: "Diagnosis only: incident #30 is already fixing this in acme/api",
      covered_by_incident_run_id: 30,
      covered_by_pr_url: "https://github.com/acme/api/pull/4",
    };
    renderAt("/agents");
    fireEvent.click(await screen.findByRole("button", { name: "Incident #31" }));
    const note = await screen.findByTestId("mode-note");
    expect(note).toHaveTextContent("Diagnosis only: incident #30 is already fixing this in acme/api.");
    expect(within(note).getByRole("link", { name: /See PR #4/ })).toHaveAttribute(
      "href", "https://github.com/acme/api/pull/4",
    );
  });

  it("runs an agent by hand", async () => {
    renderAt("/agents");
    fireEvent.click(await screen.findByRole("button", { name: "Run Nightly sweep now" }));
    await vi.waitFor(() => expect(sent("POST")?.path).toBe("/api/sre/agents/4/run"));
  });

  it("runs the top agent on Enter, but not while typing", async () => {
    renderAt("/agents");
    expect(await screen.findByRole("button", { name: "Run Nightly sweep now" })).toHaveAttribute(
      "title", "Run now (Enter)",
    );
    fireEvent.click(screen.getByRole("button", { name: /New agent/ }));
    fireEvent.keyDown(screen.getByLabelText("Name"), { key: "Enter" });
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(sent("POST")).toBeUndefined();

    fireEvent.keyDown(document.body, { key: "Enter" });
    await vi.waitFor(() => expect(sent("POST")?.path).toBe("/api/sre/agents/4/run"));
  });

  it("creates an agent for chosen repos and org playbooks only", async () => {
    renderAt("/agents");
    fireEvent.click(await screen.findByRole("button", { name: /New agent/ }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Release watch" } });
    fireEvent.change(screen.getByLabelText(/^Runs/), { target: { value: "branch_watch" } });
    fireEvent.change(screen.getByLabelText(/^Branch pattern/), { target: { value: "release/*" } });

    const [allRepos, allPlaybooks] = screen.getAllByRole("checkbox", { name: /^Every/ });
    fireEvent.click(allRepos);
    fireEvent.click(await screen.findByRole("checkbox", { name: /worker/ }));
    fireEvent.click(allPlaybooks);
    fireEvent.click(await screen.findByRole("checkbox", { name: /Null checks/ }));
    // A legacy playbook isn't one an agent may use.
    expect(screen.queryByText("Legacy one-off")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Create agent" }));
    await vi.waitFor(() => expect(sent("POST")).toBeTruthy());
    expect(sent("POST")).toMatchObject({
      path: "/api/sre/organizations/1/agents",
      body: {
        name: "Release watch", kind: "playbook_sweep", trigger: "branch_watch", branch_pattern: "release/*",
        project_ids: [3], playbook_ids: [1], execution_mode: null, enabled: true,
        code_findings_open_prs: false,
      },
    });
    // A sweep defaults to diagnoses, so the code-findings switch doesn't apply.
    expect(screen.queryByLabelText(/Open draft PRs for findings from code alone/)).not.toBeInTheDocument();
  });

  it("lets a draft-PR sweep open PRs for findings from code alone", async () => {
    renderAt("/agents");
    fireEvent.click(await screen.findByRole("button", { name: /New agent/ }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "PR sweeper" } });
    fireEvent.change(screen.getByLabelText(/^Fixes can go as far as/), { target: { value: "draft_only" } });
    fireEvent.click(screen.getByLabelText(/Open draft PRs for findings from code alone/));
    fireEvent.click(screen.getByRole("button", { name: "Create agent" }));
    await vi.waitFor(() => expect(sent("POST")).toBeTruthy());
    expect(sent("POST")?.body).toMatchObject({ execution_mode: "draft_only", code_findings_open_prs: true });
  });

  it("explains how to turn agents on when the server has them off", async () => {
    delete routes["GET /api/sre/organizations/1/agents"];
    renderAt("/agents");
    expect(await screen.findByText(/Remediation agents are turned off/)).toBeInTheDocument();
    expect(screen.getByText("SRE_REMEDIATION_AGENTS_ENABLED=true")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /New agent/ })).not.toBeInTheDocument();
  });

  it("hides admin actions from org members", async () => {
    routes["GET /api/sre/organizations"] = [{ ...org, role: "member" }];
    renderAt("/agents");
    expect(await screen.findByText("Nightly sweep")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /New agent/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Run Nightly sweep now/ })).not.toBeInTheDocument();
  });
});
