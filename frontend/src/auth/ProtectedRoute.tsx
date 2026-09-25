import type { ReactNode } from "react";
import { Navigate, useLocation } from "react-router-dom";
import { getToken } from "../api/token";

export function ProtectedRoute({ children }: { children: ReactNode }) {
  const location = useLocation();
  const token = getToken();

  if (!token) {
    sessionStorage.setItem("post_login_redirect", location.pathname);
    return <Navigate to="/login" state={{ from: location.pathname }} replace />;
  }

  return <>{children}</>;
}
