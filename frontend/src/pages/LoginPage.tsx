import { useQuery } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import type { AuthProviders } from "../api/types";
import { AuthShell, Field } from "@/components/AuthShell";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

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

  const from = (location.state as { from?: string } | null)?.from ?? "/dashboard";

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
    <AuthShell
      title="Log in"
      description="Welcome back. Sign in to see your incidents."
      footer={
        <>
          No account?{" "}
          <Link to="/register" className="font-medium text-primary hover:underline">
            Register
          </Link>
        </>
      }
    >
      <form onSubmit={handleSubmit} className="flex flex-col gap-4">
        <Field label="Email">
          <Input type="email" required value={email} onChange={(e) => setEmail(e.target.value)} />
        </Field>
        <Field label="Password">
          <Input type="password" required value={password} onChange={(e) => setPassword(e.target.value)} />
        </Field>
        {error && <p className="text-sm text-destructive">{error}</p>}
        <Button type="submit" disabled={submitting}>
          Log in
        </Button>
      </form>

      {(providers?.github || providers?.google) && (
        <>
          <div className="my-4 flex items-center gap-3 text-xs uppercase text-muted-foreground">
            <span className="h-px flex-1 bg-border" />
            or
            <span className="h-px flex-1 bg-border" />
          </div>
          <div className="flex flex-col gap-2">
            {providers.github && (
              <Button variant="outline" onClick={() => handleOAuthStart("github")}>
                Continue with GitHub
              </Button>
            )}
            {providers.google && (
              <Button variant="outline" onClick={() => handleOAuthStart("google")}>
                Continue with Google
              </Button>
            )}
          </div>
        </>
      )}
    </AuthShell>
  );
}
