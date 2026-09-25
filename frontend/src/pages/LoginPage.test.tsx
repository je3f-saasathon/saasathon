import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AuthProvider } from "../auth/AuthContext";
import { LoginPage } from "./LoginPage";

function renderLoginPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/login"]}>
        <AuthProvider>
          <LoginPage />
        </AuthProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("LoginPage", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("hides OAuth buttons for providers reported disabled", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: async () => ({ password: true, github: true, google: false }),
      }),
    );

    renderLoginPage();

    await waitFor(() => {
      expect(screen.getByText("Continue with GitHub")).toBeInTheDocument();
    });
    expect(screen.queryByText("Continue with Google")).not.toBeInTheDocument();
  });

  it("shows no OAuth buttons when all providers are disabled", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: async () => ({ password: true, github: false, google: false }),
      }),
    );

    renderLoginPage();

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Log in" })).toBeInTheDocument();
    });
    expect(screen.queryByText("Continue with GitHub")).not.toBeInTheDocument();
    expect(screen.queryByText("Continue with Google")).not.toBeInTheDocument();
  });
});
