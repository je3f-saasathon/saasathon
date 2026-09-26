import { useMutation } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { CheckCircle2 } from "lucide-react";
import { useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { AuthShell, Field } from "@/components/AuthShell";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

// `buggly login` opens this page with ?code=; approving it lets the CLI's next poll get a token.
export function CliLoginPage() {
  const [params] = useSearchParams();
  const { user } = useAuth();
  const [code, setCode] = useState(params.get("code") ?? "");
  const approve = useMutation({
    mutationFn: (user_code: string) => api.post("/auth/cli/approve", { user_code }),
  });

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    approve.mutate(code.trim());
  }

  if (approve.isSuccess) {
    return (
      <AuthShell title="CLI signed in">
        <p className="flex items-center gap-2 text-sm text-muted-foreground">
          <CheckCircle2 className="h-4 w-4 text-primary" />
          You can close this tab and go back to your terminal.
        </p>
      </AuthShell>
    );
  }

  return (
    <AuthShell
      title="Sign in the buggly CLI"
      description={`Check the code matches the one in your terminal. The CLI will act as ${user?.email ?? "you"}.`}
    >
      <form onSubmit={handleSubmit} className="flex flex-col gap-4">
        <Field label="Code">
          <Input
            required
            autoComplete="off"
            className="font-mono uppercase tracking-widest"
            placeholder="ABCD-EFGH"
            value={code}
            onChange={(e) => setCode(e.target.value)}
          />
        </Field>
        {approve.error && <p className="text-sm text-destructive">{approve.error.message}</p>}
        <Button type="submit" disabled={approve.isPending}>
          Approve
        </Button>
      </form>
    </AuthShell>
  );
}
