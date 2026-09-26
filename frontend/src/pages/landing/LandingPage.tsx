import { useEffect, useRef, useState } from "react";
import { ArrowRight, Bug, GitPullRequest, Radar, ScanSearch } from "lucide-react";
import { Link } from "react-router-dom";

import { useAuth } from "@/auth/AuthContext";
import { ThemeToggle, useTheme, type Theme } from "@/components/theme";

const steps = [
  {
    icon: Radar,
    title: "Detect",
    body: "Your monitoring fires a webhook the moment an error spikes. Noise is filtered out before anything runs.",
  },
  {
    icon: ScanSearch,
    title: "Diagnose",
    body: "The agent classifies the bug, finds the playbook that fixed it last time, or writes a new one.",
  },
  {
    icon: GitPullRequest,
    title: "Fix",
    body: "In a locked-down sandbox it edits the code, runs your tests and opens a pull request for you to approve.",
  },
];

function prefersReducedMotion() {
  return typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
}

/** The three.js bug sweep, or a static glow when WebGL isn't available. */
function HeroScene({ theme }: { theme: Theme }) {
  const ref = useRef<HTMLDivElement>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    const container = ref.current;
    if (!container) return;
    let dispose: (() => void) | undefined;
    let cancelled = false;
    // Loaded on demand so three.js stays out of the main app bundle.
    import("./bugSweepScene")
      .then(({ mountBugSweep }) => {
        if (cancelled) return;
        dispose = mountBugSweep(container, {
          theme,
          reducedMotion: prefersReducedMotion(),
        });
      })
      .catch(() => {
        if (!cancelled) setFailed(true);
      });
    return () => {
      cancelled = true;
      dispose?.();
    };
  }, [theme]);

  return (
    <div ref={ref} className="absolute inset-0" aria-hidden="true">
      {failed && (
        <div className="absolute inset-0 bg-[radial-gradient(ellipse_at_50%_60%,rgba(236,116,89,0.2),transparent_60%),radial-gradient(ellipse_at_30%_40%,rgba(230,84,185,0.12),transparent_55%)]" />
      )}
    </div>
  );
}

export function LandingPage() {
  const { user } = useAuth();
  const { theme } = useTheme();

  const primary = user
    ? { to: "/dashboard", label: "Open dashboard" }
    : { to: "/register", label: "Get started" };

  return (
    <div className="min-h-screen bg-[#efe9e8] text-stone-900 dark:bg-[#120a0c] dark:text-stone-100">
      <section className="relative h-[100svh] min-h-[560px] overflow-hidden">
        <HeroScene theme={theme} />
        {/* Vignettes keep the copy readable over the scene. */}
        <div className="pointer-events-none absolute inset-0 bg-gradient-to-b from-[#efe9e8]/90 via-transparent to-[#efe9e8] dark:from-[#120a0c]/90 dark:to-[#120a0c]" />
        <div className="pointer-events-none absolute inset-y-0 left-0 w-full bg-gradient-to-r from-[#efe9e8]/85 via-[#efe9e8]/30 to-transparent dark:from-[#120a0c]/80 dark:via-[#120a0c]/20 md:w-2/3" />

        <header className="relative z-10 mx-auto flex max-w-6xl items-center justify-between px-6 py-5">
          <Link to="/" className="flex items-center gap-2 font-semibold tracking-tight">
            <Bug className="h-5 w-5 text-[#ce4c32] dark:text-orange-300" />
            buggly
          </Link>
          <nav className="flex items-center gap-2 text-sm">
            {user ? (
              <Link
                to="/dashboard"
                className="rounded-md px-3 py-1.5 text-stone-600 hover:text-stone-900 dark:text-stone-300 dark:hover:text-white"
              >
                Dashboard
              </Link>
            ) : (
              <Link to="/login" className="rounded-md px-3 py-1.5 text-stone-600 hover:text-stone-900 dark:text-stone-300 dark:hover:text-white">
                Log in
              </Link>
            )}
            <Link
              to={primary.to}
              className="rounded-md bg-black/5 px-3 py-1.5 font-medium text-stone-900 ring-1 ring-black/10 backdrop-blur hover:bg-black/10 dark:bg-white/10 dark:text-white dark:ring-white/15 dark:hover:bg-white/15"
            >
              {primary.label}
            </Link>
            <ThemeToggle className="text-stone-600 hover:bg-black/5 hover:text-stone-900 dark:text-stone-300 dark:hover:bg-white/10 dark:hover:text-white" />
          </nav>
        </header>

        <div className="pointer-events-none relative z-10 mx-auto flex h-[calc(100%-76px)] max-w-6xl flex-col justify-end px-6 pb-16 md:justify-center md:pb-0">
          <div className="pointer-events-auto max-w-xl space-y-6">
            <p className="inline-flex items-center gap-2 rounded-full border border-orange-700/20 bg-orange-600/5 px-3 py-1 text-xs font-medium text-orange-800 backdrop-blur dark:border-orange-300/20 dark:bg-orange-300/5 dark:text-orange-200">
              <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-orange-400" />
              Autonomous incident response
            </p>
            <h1 className="text-4xl font-semibold leading-[1.05] tracking-tight sm:text-6xl">
              Production bugs,
              <br />
              <span className="bg-gradient-to-r from-orange-600 via-[#ce4c32] to-pink-600 bg-clip-text dark:from-amber-200 dark:via-orange-300 dark:to-pink-400 text-transparent">
                fixed before you wake up.
              </span>
            </h1>
            <p className="text-base text-stone-700 sm:text-lg dark:text-stone-300">
              An SRE agent that catches errors as they happen, traces them to the code, and opens
              a tested pull request, so your on-call rotation reviews fixes instead of writing them.
            </p>
            <div className="flex flex-wrap items-center gap-3">
              <Link
                to={primary.to}
                className="inline-flex items-center gap-2 rounded-md bg-[#ce4c32] px-5 py-2.5 text-sm font-semibold text-white shadow-[0_0_40px_-8px_rgba(206,76,50,0.6)] transition hover:bg-[#b9432b] dark:bg-[#ec7459] dark:text-[#120a0c] dark:shadow-[0_0_40px_-8px_rgba(236,116,89,0.8)] dark:hover:bg-[#f08a73]"
              >
                {primary.label} <ArrowRight className="h-4 w-4" />
              </Link>
              <a
                href="#how-it-works"
                className="rounded-md px-4 py-2.5 text-sm font-medium text-stone-700 ring-1 ring-black/10 hover:text-stone-900 hover:ring-black/25 dark:text-stone-300 dark:ring-white/10 dark:hover:text-white dark:hover:ring-white/25"
              >
                How it works
              </a>
            </div>
          </div>
        </div>

      </section>

      <section id="how-it-works" className="mx-auto max-w-6xl px-6 py-24">
        <h2 className="text-2xl font-semibold tracking-tight sm:text-3xl">
          From alert to pull request, on its own.
        </h2>
        <p className="mt-3 max-w-2xl text-stone-600 dark:text-stone-400">
          Every step is traced, every token is counted, and nothing merges without a human saying
          yes.
        </p>
        <div className="mt-12 grid gap-4 md:grid-cols-3">
          {steps.map(({ icon: Icon, title, body }, i) => (
            <div
              key={title}
              className="relative rounded-xl border border-black/10 bg-gradient-to-b from-white/60 to-transparent p-6 dark:border-white/10 dark:from-white/[0.04]"
            >
              <div className="mb-4 flex items-center gap-3">
                <span className="flex h-9 w-9 items-center justify-center rounded-lg bg-orange-600/10 ring-1 ring-orange-600/20 dark:bg-orange-300/10 dark:ring-orange-300/20">
                  <Icon className="h-4 w-4 text-[#ce4c32] dark:text-orange-300" />
                </span>
                <span className="font-mono text-xs text-stone-500">0{i + 1}</span>
              </div>
              <h3 className="font-semibold">{title}</h3>
              <p className="mt-2 text-sm leading-relaxed text-stone-600 dark:text-stone-400">{body}</p>
            </div>
          ))}
        </div>
      </section>

      <section className="border-t border-black/5 dark:border-white/5">
        <div className="mx-auto flex max-w-6xl flex-col items-start justify-between gap-6 px-6 py-16 sm:flex-row sm:items-center">
          <div>
            <h2 className="text-xl font-semibold">Point it at a repo and watch.</h2>
            <p className="mt-1 text-sm text-stone-600 dark:text-stone-400">
              Connect GitHub, pick your models, and send your first alert.
            </p>
          </div>
          <Link
            to={primary.to}
            className="inline-flex items-center gap-2 rounded-md bg-[#241418] px-5 py-2.5 text-sm font-semibold text-[#f3e6e2] hover:bg-[#3a2228] dark:bg-[#f3e6e2] dark:text-[#120a0c] dark:hover:bg-[#ebd9d6]"
          >
            {primary.label} <ArrowRight className="h-4 w-4" />
          </Link>
        </div>
      </section>
    </div>
  );
}
