import { useQuery } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import type { AuthProviders } from "../api/types";

export function LoginPage() {
  const { loginWithPassword, startOAuth } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const { data: providers } = useQuery({
    queryKey: ["auth-providers"],
    queryFn: () => api.get<AuthProviders>("/auth/providers", { skipAuth: true }),
    retry: false,
  });

  const from = (location.state as { from?: string } | null)?.from ?? "/";

  function handleOAuthStart(provider: "github" | "google") {
    sessionStorage.setItem("post_login_redirect", from);
    startOAuth(provider);
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await loginWithPassword({ email, password });
      navigate(from, { replace: true });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Login failed");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="mx-auto mt-16 max-w-sm">
      <h1 className="mb-6 text-2xl font-semibold">Log in</h1>
      <form onSubmit={handleSubmit} className="flex flex-col gap-3">
        <label className="flex flex-col gap-1 text-sm">
          Email
          <input
            type="email"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className="rounded border px-3 py-2"
          />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          Password
          <input
            type="password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="rounded border px-3 py-2"
          />
        </label>
        {error && <p className="text-sm text-red-600">{error}</p>}
        <button
          type="submit"
          disabled={submitting}
          className="rounded bg-blue-600 px-3 py-2 text-white hover:bg-blue-700 disabled:opacity-50"
        >
          Log in
        </button>
      </form>

      {(providers?.github || providers?.google) && (
        <div className="mt-4 flex flex-col gap-2">
          {providers.github && (
            <button
              onClick={() => handleOAuthStart("github")}
              className="rounded border px-3 py-2 hover:bg-gray-50"
            >
              Continue with GitHub
            </button>
          )}
          {providers.google && (
            <button
              onClick={() => handleOAuthStart("google")}
              className="rounded border px-3 py-2 hover:bg-gray-50"
            >
              Continue with Google
            </button>
          )}
        </div>
      )}

      <p className="mt-4 text-sm text-gray-600">
        No account? <Link to="/register" className="text-blue-600">Register</Link>
      </p>
    </div>
  );
}
