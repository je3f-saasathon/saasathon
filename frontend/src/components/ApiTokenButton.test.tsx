import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiTokenButton } from "./ApiTokenButton";

describe("ApiTokenButton", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("creates a token on request and shows it once, ready to copy", async () => {
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      expect(`${init?.method} ${new URL(url).pathname}`).toBe("POST /api/auth/tokens");
      return { ok: true, status: 200, json: async () => ({ token: "tok-123", expires_at: "2026-10-26T00:00:00Z" }) };
    });
    vi.stubGlobal("fetch", fetchMock);
    const writeText = vi.fn();
    vi.stubGlobal("navigator", { clipboard: { writeText } });

    render(
      <QueryClientProvider client={new QueryClient()}>
        <ApiTokenButton />
      </QueryClientProvider>,
    );
    fireEvent.click(screen.getByRole("button", { name: "API token" }));
    expect(fetchMock).not.toHaveBeenCalled(); // nothing is created until asked
    fireEvent.click(screen.getByRole("button", { name: "Create a token" }));

    expect(await screen.findByDisplayValue("tok-123")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Copy API token" }));
    expect(writeText).toHaveBeenCalledWith("tok-123");
    expect(screen.getByText(/Shown only once/)).toBeInTheDocument();
  });
});
