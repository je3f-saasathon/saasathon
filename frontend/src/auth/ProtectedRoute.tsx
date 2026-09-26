import type { ReactNode } from "react";
import { Navigate, useLocation } from "react-router-dom";
import { getToken } from "../api/token";

export function ProtectedRoute({ children }: { children: ReactNode }) {
  const location = useLocation();
  const token = getToken();

  if (!token) {
    // Keep the query too: /cli?code=... must survive the trip through login.
    const from = location.pathname + location.search;
    sessionStorage.setItem("post_login_redirect", from);
    return <Navigate to="/login" state={{ from }} replace />;
  }

  return <>{children}</>;
}
