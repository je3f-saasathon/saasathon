import { fireEvent, render, screen } from "@testing-library/react";
import { DashboardPage } from "./DashboardPage";
import { mockLogs, mockStats } from "./mock-logs";

describe("DashboardPage", () => {
  it("renders summary cards and the first page of logs", () => {
    render(<DashboardPage />);
    expect(screen.getByText("Active Errors")).toBeInTheDocument();
    expect(screen.getByText(mockStats.activeErrors.toString())).toBeInTheDocument();
    expect(screen.getByText(`${mockLogs.length} row(s)`)).toBeInTheDocument();
    expect(screen.getByText(mockLogs[0].message)).toBeInTheDocument();
  });

  it("filters rows by the message search input", () => {
    render(<DashboardPage />);
    fireEvent.change(screen.getByPlaceholderText("Search messages..."), {
      target: { value: "bad_verification_code" },
    });
    const matches = mockLogs.filter((log) => log.message.includes("bad_verification_code"));
    expect(screen.getByText(`${matches.length} row(s)`)).toBeInTheDocument();
  });
});
