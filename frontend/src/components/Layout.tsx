import { Bug } from "lucide-react";
import { Link, NavLink, Outlet, useNavigate } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { ApiTokenButton } from "./ApiTokenButton";
import { HealthIndicator } from "./HealthIndicator";
import { ThemeToggle } from "./theme";

const navItems = [
  { to: "/dashboard", label: "Dashboard" },
  { to: "/settings", label: "Settings" },
];

export function Layout() {
  const { user, logout } = useAuth();
  const navigate = useNavigate();

  async function handleLogout() {
    await logout();
    navigate("/login");
  }

  return (
    <div className="min-h-screen bg-background">
      <header className="sticky top-0 z-30 flex items-center justify-between border-b bg-background/80 px-6 py-3 backdrop-blur">
        <nav className="flex items-center gap-6">
          <Link to="/" className="flex items-center gap-2 font-semibold tracking-tight">
            <Bug className="h-5 w-5 text-primary" />
            buggly
          </Link>
          {navItems.map(({ to, label }) => (
            <NavLink
              key={to}
              to={to}
              className={({ isActive }) =>
                cn(
                  "text-sm transition-colors hover:text-foreground",
                  isActive ? "font-medium text-foreground" : "text-muted-foreground",
                )
              }
            >
              {label}
            </NavLink>
          ))}
        </nav>
        <div className="flex items-center gap-4">
          <HealthIndicator />
          <ThemeToggle />
          {user && (
            <div className="flex items-center gap-3">
              {user.avatar_url ? (
                <img src={user.avatar_url} alt={user.name} className="h-8 w-8 rounded-full ring-1 ring-border" />
              ) : (
                <div className="flex h-8 w-8 items-center justify-center rounded-full bg-primary/15 text-sm font-medium text-primary ring-1 ring-primary/30">
                  {user.name?.[0]?.toUpperCase() ?? "?"}
                </div>
              )}
              <span className="text-sm font-medium">{user.name}</span>
              <ApiTokenButton />
              <Button variant="outline" size="sm" onClick={handleLogout}>
                Log out
              </Button>
            </div>
          )}
        </div>
      </header>
      <main className="p-6">
        <Outlet />
      </main>
    </div>
  );
}
