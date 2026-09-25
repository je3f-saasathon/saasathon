import type { ReactNode } from "react";
import { Bug } from "lucide-react";
import { Link } from "react-router-dom";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { ThemeToggle } from "./theme";

// Centred card with the landing page's glow, shared by login and register.
export function AuthShell({
  title,
  description,
  children,
  footer,
}: {
  title: string;
  description?: string;
  children: ReactNode;
  footer?: ReactNode;
}) {
  return (
    <div className="relative flex min-h-screen flex-col overflow-hidden bg-background">
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-0 bg-[radial-gradient(ellipse_at_50%_30%,hsl(var(--primary)/0.12),transparent_60%)]"
      />
      <header className="relative z-10 flex items-center justify-between px-6 py-5">
        <Link to="/" className="flex items-center gap-2 font-semibold tracking-tight">
          <Bug className="h-5 w-5 text-primary" />
          SRE Agent
        </Link>
        <ThemeToggle />
      </header>
      <main className="relative z-10 flex flex-1 items-start justify-center px-4 pt-10 sm:pt-16">
        <div className="w-full max-w-sm">
          <Card className="bg-card/80 backdrop-blur">
            <CardHeader>
              <CardTitle className="text-2xl">{title}</CardTitle>
              {description && <CardDescription>{description}</CardDescription>}
            </CardHeader>
            <CardContent>{children}</CardContent>
          </Card>
          {footer && <p className="mt-4 text-center text-sm text-muted-foreground">{footer}</p>}
        </div>
      </main>
    </div>
  );
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="flex flex-col gap-1.5 text-sm font-medium">
      {label}
      {children}
    </label>
  );
}
