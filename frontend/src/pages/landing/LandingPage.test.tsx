import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AuthProvider } from "@/auth/AuthContext";
import { LandingPage } from "./LandingPage";

function renderLanding() {
  return render(
    <MemoryRouter>
      <AuthProvider>
        <LandingPage />
      </AuthProvider>
    </MemoryRouter>,
  );
}

describe("LandingPage", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  // jsdom has no WebGL: the page must still render, with the static fallback behind it.
  it("renders the hero and sign-up calls to action without WebGL", () => {
    vi.stubGlobal("fetch", vi.fn());
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    renderLanding();
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(/Production bugs/);
    expect(screen.getAllByRole("link", { name: /Get started/ })[0]).toHaveAttribute("href", "/register");
    expect(screen.getByRole("link", { name: "Log in" })).toHaveAttribute("href", "/login");
    expect(screen.getByTestId("squash-count")).toHaveTextContent("0");
  });
});
