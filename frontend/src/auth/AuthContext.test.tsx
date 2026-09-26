import { render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AuthProvider, useAuth } from "./AuthContext";

let auth: ReturnType<typeof useAuth>;

function Probe() {
  auth = useAuth();
  return null;
}

async function loadWith(fetchImpl: () => Promise<Response>) {
  vi.stubGlobal("fetch", vi.fn(fetchImpl));
  render(
    <AuthProvider>
      <Probe />
    </AuthProvider>,
  );
  await waitFor(() => expect(auth.isLoading).toBe(false));
}

describe("AuthProvider", () => {
  beforeEach(() => {
    localStorage.clear();
    localStorage.setItem("auth_token", "test-token");
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("keeps the token when /auth/me is cut off, e.g. by a reload", async () => {
    await loadWith(() => Promise.reject(new DOMException("The user aborted a request.", "AbortError")));
    expect(localStorage.getItem("auth_token")).toBe("test-token");
  });

  it("keeps the token when /auth/me fails with a server error", async () => {
    await loadWith(async () => new Response("", { status: 502 }));
    expect(localStorage.getItem("auth_token")).toBe("test-token");
  });

  it("clears the token when /auth/me says it's invalid", async () => {
    await loadWith(async () => new Response("", { status: 401 }));
    expect(localStorage.getItem("auth_token")).toBeNull();
  });
});
