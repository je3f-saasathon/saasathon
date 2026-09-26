import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AuthProvider } from "../auth/AuthContext";
import { ThemeProvider } from "../components/theme";
import { CliLoginPage } from "./CliLoginPage";

function renderAt(url: string) {
  return render(
    <ThemeProvider>
      <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter initialEntries={[url]}>
        <AuthProvider>
          <Routes>
            <Route path="/cli" element={<CliLoginPage />} />
          </Routes>
        </AuthProvider>
      </MemoryRouter>
      </QueryClientProvider>
    </ThemeProvider>,
  );
}

function reply(status: number, body: unknown) {
  return { ok: status < 400, status, statusText: "", json: async () => body };
}

describe("CliLoginPage", () => {
  beforeEach(() => localStorage.setItem("auth_token", "tok"));
  afterEach(() => {
    vi.unstubAllGlobals();
    localStorage.clear();
  });

  it("approves the code from the URL", async () => {
    const fetchMock = vi.fn(async (url: string) =>
      url.endsWith("/auth/me")
        ? reply(200, { user: { id: 1, email: "a@example.com", name: "Ada" } })
        : reply(200, { ok: true }),
    );
    vi.stubGlobal("fetch", fetchMock);
    renderAt("/cli?code=ABCD-EFGH");

    expect(screen.getByDisplayValue("ABCD-EFGH")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(await screen.findByText(/go back to your terminal/)).toBeInTheDocument();
    const [, init] = fetchMock.mock.calls.find(([u]) => u.endsWith("/auth/cli/approve"))! as unknown as [
      string,
      RequestInit,
    ];
    expect(JSON.parse(init.body as string)).toEqual({ user_code: "ABCD-EFGH" });
  });

  it("shows why a code was refused", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) =>
        url.endsWith("/auth/me") ? reply(200, { user: { id: 1 } }) : reply(404, { detail: "That code is unknown" }),
      ),
    );
    renderAt("/cli?code=NOPE-NOPE");
    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(screen.getByText("That code is unknown")).toBeInTheDocument());
  });
});
