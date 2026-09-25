import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AuthProvider, useAuth } from "../auth/AuthContext";
import { AuthCallbackPage } from "./AuthCallbackPage";

function WhoAmI() {
  const { user, isLoading } = useAuth();
  if (isLoading) return <div>loading</div>;
  return <div>{user ? `Logged in as ${user.name}` : "No user"}</div>;
}

function renderCallback(hash: string) {
  window.history.pushState(null, "", `/auth/callback${hash}`);
  return render(
    <MemoryRouter initialEntries={["/auth/callback"]}>
      <AuthProvider>
        <Routes>
          <Route path="/auth/callback" element={<AuthCallbackPage />} />
          <Route path="/dashboard" element={<WhoAmI />} />
        </Routes>
      </AuthProvider>
    </MemoryRouter>,
  );
}

describe("AuthCallbackPage", () => {
  beforeEach(() => {
    localStorage.clear();
    sessionStorage.clear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("populates the auth context user after storing the OAuth token", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: async () => ({ user: { id: "1", email: "a@example.com", name: "Ada", role: "member" } }),
      }),
    );

    renderCallback("#token=abc123");

    await waitFor(() => {
      expect(screen.getByText("Logged in as Ada")).toBeInTheDocument();
    });
    expect(localStorage.getItem("auth_token")).toBe("abc123");
  });

  it("shows an error and clears the token when /auth/me fails after storing it", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 401,
        statusText: "Unauthorized",
        json: async () => ({ detail: "Unauthorized" }),
      }),
    );

    renderCallback("#token=badtoken");

    await waitFor(() => {
      expect(screen.getByText("Failed to load account after login")).toBeInTheDocument();
    });
    expect(localStorage.getItem("auth_token")).toBeNull();
  });
});
