import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import { getToken, setToken } from "../api/token";

export function AuthCallbackPage() {
  const navigate = useNavigate();
  const { refreshUser } = useAuth();
  const [error, setError] = useState<string | null>(null);
  const ranOnce = useRef(false);

  useEffect(() => {
    if (ranOnce.current) return;
    ranOnce.current = true;

    async function run() {
      const hash = window.location.hash;
      const match = hash.match(/token=([^&]+)/);

      if (!match) {
        setError("Missing token in callback URL");
        return;
      }

      const token = decodeURIComponent(match[1]);
      setToken(token);

      // Clear the fragment from the URL without adding a history entry.
      window.history.replaceState(null, "", window.location.pathname);

      await refreshUser();

      if (!getToken()) {
        setError("Failed to load account after login");
        return;
      }

      const redirectTo = sessionStorage.getItem("post_login_redirect") || "/";
      sessionStorage.removeItem("post_login_redirect");
      navigate(redirectTo, { replace: true });
    }

    run();
  }, [navigate]);

  return (
    <div className="mx-auto mt-16 max-w-sm text-center">
      {error ? <p className="text-red-600">{error}</p> : <p>Signing you in...</p>}
    </div>
  );
}
