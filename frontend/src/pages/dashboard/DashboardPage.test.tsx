import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DashboardPage } from "./DashboardPage";

const usage = {
  calls: 3,
  input_tokens: 1000,
  output_tokens: 234,
  total_tokens: 1234,
  platform_tokens: 1234,
  models: ["gpt-5.4-mini", "gpt-5.5"],
  by_step: [
    { step: "bug_classification", provider: "openai", model: "gpt-5.4-mini", billed_to: "platform", calls: 1, input_tokens: 400, output_tokens: 34 },
    { step: "playbook_execution", provider: "openai", model: "gpt-5.5", billed_to: "platform", calls: 2, input_tokens: 600, output_tokens: 200 },
  ],
};

const run = {
  id: 7,
  project_id: 2,
  project_name: "django-buggy-app",
  trace_id: "abc123",
  uptrace_exception_id: "",
  temporal_workflow_id: "sre-incident-2-abc123",
  status: "succeeded",
  classification: { category: "code_bug", severity: "high", summary: "ZeroDivisionError in checkout", suspected_files: ["shop/views.py"] },
  matched_playbook_id: 3,
  created_playbook_id: null,
  playbook_run_id: 5,
  diagnosis_report: "",
  error_message: "",
  created_at: "2026-09-25T14:58:47.203Z",
  updated_at: "2026-09-25T15:02:00.000Z",
  playbook: { id: 3, title: "Guard division by zero", status: "confirmed", source: "matched" },
  pr_url: "https://github.com/je3f-saasathon/django-buggy-app/pull/1",
  playbook_run_status: "succeeded",
  execution_mode: "draft_only",
  generate_tests: false,
  usage,
};

const responses: Record<string, unknown> = {
  "/api/sre/incident-runs": { runs: [run], total: 1 },
  "/api/sre/playbooks/3": {
    id: 3, project_id: 2, title: "Guard division by zero", description: "", keywords: [],
    steps: [{ type: "run_command", command: "pytest -x" }], status: "confirmed",
    execution_mode_override: null, consecutive_failure_count: 0, source_incident_run_id: null,
    created_at: run.created_at, updated_at: run.created_at,
  },
  "/api/sre/playbook-runs/5": {
    id: 5, incident_run_id: 7, playbook_id: 3, execution_mode: "draft_only", status: "succeeded",
    approved_by_id: 1, approved_at: run.updated_at, pr_url: run.pr_url, branch_name: "sre/incident-7-a1",
    attempts: [{ attempt_number: 1, outcome: "succeeded", summary: "Returned 400 when quantity is 0", error_output: "", generated_steps: [], branch_name: "sre/incident-7-a1", langfuse_trace_id: "", created_at: run.created_at }],
  },
};

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <DashboardPage />
    </QueryClientProvider>,
  );
}

describe("DashboardPage", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        const path = new URL(url).pathname;
        return { ok: true, status: 200, json: async () => responses[path] };
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("lists incidents with PR link, playbook, model and tokens", async () => {
    renderPage();
    expect(await screen.findByText("ZeroDivisionError in checkout")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /PR #1/ })).toHaveAttribute("href", run.pr_url);
    expect(screen.getByText("Guard division by zero")).toBeInTheDocument();
    expect(screen.getByText("gpt-5.4-mini")).toBeInTheDocument();
    expect(screen.getByText("gpt-5.5")).toBeInTheDocument();
    expect(screen.getAllByText((1234).toLocaleString()).length).toBeGreaterThan(0);
    expect(screen.getByText("tests off · our key")).toBeInTheDocument();
  });

  it("shows details on row click, with the diagnosis hidden until asked for", async () => {
    renderPage();
    fireEvent.click(await screen.findByText("ZeroDivisionError in checkout"));
    expect(await screen.findByText("pytest -x")).toBeInTheDocument();
    expect(await screen.findByText("Attempt 1")).toBeInTheDocument();
    expect(screen.getByText("playbook execution")).toBeInTheDocument();
    expect(screen.queryByText("shop/views.py")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Show diagnosis/ }));
    expect(screen.getByText("shop/views.py")).toBeInTheDocument();
    expect(screen.getAllByText("Returned 400 when quantity is 0").length).toBeGreaterThan(0);
    // Decided already: a plain PR link, no review prompt.
    expect(screen.getByRole("link", { name: "Open PR" })).toHaveAttribute("href", run.pr_url);
    expect(screen.queryByText(/waiting for review on GitHub/)).not.toBeInTheDocument();
  });

  it("sends a draft-only run awaiting approval to GitHub for review", async () => {
    responses["/api/sre/incident-runs"] = {
      runs: [{ ...run, status: "awaiting_approval", playbook_run_status: "pending_approval" }],
      total: 1,
    };
    renderPage();
    fireEvent.click(await screen.findByText("ZeroDivisionError in checkout"));
    expect(screen.getByRole("link", { name: "Review on GitHub" })).toHaveAttribute("href", run.pr_url);
    expect(screen.getByText(/waiting for review on GitHub/)).toBeInTheDocument();
    responses["/api/sre/incident-runs"] = { runs: [run], total: 1 };
  });
});
