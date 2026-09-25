import { Link, Outlet, useNavigate } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import { HealthIndicator } from "./HealthIndicator";

export function Layout() {
  const { user, logout } = useAuth();
  const navigate = useNavigate();

  async function handleLogout() {
    await logout();
    navigate("/login");
  }

  return (
    <div className="min-h-screen bg-gray-50">
      <header className="flex items-center justify-between border-b bg-white px-6 py-3">
        <nav className="flex items-center gap-6">
          <Link to="/" className="text-lg font-semibold">
            App
          </Link>
          <Link to="/" className="text-sm text-gray-600 hover:text-gray-900">
            Items
          </Link>
          <Link to="/dashboard" className="text-sm text-gray-600 hover:text-gray-900">
            Dashboard
          </Link>
          <Link to="/settings" className="text-sm text-gray-600 hover:text-gray-900">
            Settings
          </Link>
        </nav>
        <div className="flex items-center gap-4">
          <HealthIndicator />
          {user && (
            <div className="flex items-center gap-3">
              {user.avatar_url ? (
                <img src={user.avatar_url} alt={user.name} className="h-8 w-8 rounded-full" />
              ) : (
                <div className="flex h-8 w-8 items-center justify-center rounded-full bg-gray-300 text-sm">
                  {user.name?.[0]?.toUpperCase() ?? "?"}
                </div>
              )}
              <span className="text-sm font-medium">{user.name}</span>
              <button
                onClick={handleLogout}
                className="rounded bg-gray-200 px-3 py-1 text-sm hover:bg-gray-300"
              >
                Log out
              </button>
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
