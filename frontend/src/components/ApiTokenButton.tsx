import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { Copy, KeyRound, X } from "lucide-react";

import { api } from "@/api/client";
import type { IssuedToken } from "@/api/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

/** Mints a separate API token for scripts (the API, the SRE lab) and shows it once. */
export function ApiTokenButton() {
  const [open, setOpen] = useState(false);
  const [copied, setCopied] = useState(false);
  const issue = useMutation({ mutationFn: () => api.post<IssuedToken>("/auth/tokens") });

  function close() {
    setOpen(false);
    setCopied(false);
    issue.reset();
  }

  return (
    <div className="relative">
      <Button
        variant="ghost"
        size="icon"
        aria-label="API token"
        title="API token for scripts"
        onClick={() => (open ? close() : setOpen(true))}
      >
        <KeyRound />
      </Button>
      {open && (
        <div className="absolute right-0 top-11 z-40 w-96 space-y-3 rounded-md border bg-background p-4 text-sm shadow-lg">
          <div className="flex items-start justify-between gap-2">
            <p className="font-medium">API token</p>
            <Button variant="ghost" size="icon" className="h-6 w-6" onClick={close} aria-label="Close">
              <X />
            </Button>
          </div>
          <p className="text-muted-foreground">
            For scripts and the API (<code>Authorization: Bearer &lt;token&gt;</code>), e.g. the SRE
            lab's <code>SRE_API_TOKEN</code>. It acts as you, lasts 30 days, and keeps working after
            you log out here. Treat it like a password.
          </p>
          {issue.data ? (
            <div className="space-y-2">
              <div className="flex gap-2">
                <Input readOnly value={issue.data.token} className="font-mono text-xs" aria-label="New API token" />
                <Button
                  variant="outline"
                  size="icon"
                  aria-label="Copy API token"
                  onClick={() => {
                    navigator.clipboard?.writeText(issue.data.token);
                    setCopied(true);
                  }}
                >
                  <Copy />
                </Button>
              </div>
              <p className="text-xs text-muted-foreground">
                {copied ? "Copied. " : ""}Shown only once. Expires{" "}
                {new Date(issue.data.expires_at).toLocaleDateString()}.
              </p>
            </div>
          ) : (
            <Button size="sm" disabled={issue.isPending} onClick={() => issue.mutate()}>
              {issue.isPending ? "Creating..." : "Create a token"}
            </Button>
          )}
          {issue.isError && <p className="text-destructive">Could not create a token.</p>}
        </div>
      )}
    </div>
  );
}
