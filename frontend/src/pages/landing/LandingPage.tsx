import { useEffect, useRef, useState } from "react";
import { ArrowRight, Bug, GitPullRequest, Radar, ScanSearch } from "lucide-react";
import { Link } from "react-router-dom";

import { useAuth } from "@/auth/AuthContext";

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
function HeroScene({ onSquash }: { onSquash: (total: number) => void }) {
  const ref = useRef<HTMLDivElement>(null);
  const [failed, setFailed] = useState(false);
  const onSquashRef = useRef(onSquash);
  onSquashRef.current = onSquash;

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
          onSquash: (total) => onSquashRef.current(total),
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
  }, []);

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
  const [squashed, setSquashed] = useState(0);

  const primary = user
    ? { to: "/dashboard", label: "Open dashboard" }
    : { to: "/register", label: "Get started" };

  return (
    <div className="min-h-screen bg-[#120a0c] text-stone-100">
      <section className="relative h-[100svh] min-h-[560px] overflow-hidden">
        <HeroScene onSquash={setSquashed} />
        {/* Vignettes keep the copy readable over the scene. */}
        <div className="pointer-events-none absolute inset-0 bg-gradient-to-b from-[#120a0c]/90 via-transparent to-[#120a0c]" />
        <div className="pointer-events-none absolute inset-y-0 left-0 w-full bg-gradient-to-r from-[#120a0c]/80 via-[#120a0c]/20 to-transparent md:w-2/3" />

        <header className="relative z-10 mx-auto flex max-w-6xl items-center justify-between px-6 py-5">
          <Link to="/" className="flex items-center gap-2 font-semibold tracking-tight">
            <Bug className="h-5 w-5 text-orange-300" />
            buggly
          </Link>
          <nav className="flex items-center gap-2 text-sm">
            {user ? (
              <Link
                to="/dashboard"
                className="rounded-md px-3 py-1.5 text-stone-300 hover:text-white"
              >
                Dashboard
              </Link>
            ) : (
              <Link to="/login" className="rounded-md px-3 py-1.5 text-stone-300 hover:text-white">
                Log in
              </Link>
            )}
            <Link
              to={primary.to}
              className="rounded-md bg-white/10 px-3 py-1.5 font-medium text-white ring-1 ring-white/15 backdrop-blur hover:bg-white/15"
            >
              {primary.label}
            </Link>
          </nav>
        </header>

        <div className="pointer-events-none relative z-10 mx-auto flex h-[calc(100%-76px)] max-w-6xl flex-col justify-end px-6 pb-16 md:justify-center md:pb-0">
          <div className="pointer-events-auto max-w-xl space-y-6">
            <p className="inline-flex items-center gap-2 rounded-full border border-orange-300/20 bg-orange-300/5 px-3 py-1 text-xs font-medium text-orange-200 backdrop-blur">
              <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-orange-400" />
              Autonomous incident response
            </p>
            <h1 className="text-4xl font-semibold leading-[1.05] tracking-tight sm:text-6xl">
              Production bugs,
              <br />
              <span className="bg-gradient-to-r from-amber-200 via-orange-300 to-pink-400 bg-clip-text text-transparent">
                fixed before you wake up.
              </span>
            </h1>
            <p className="text-base text-stone-300 sm:text-lg">
              An SRE agent that catches errors as they happen, traces them to the code, and opens
              a tested pull request, so your on-call rotation reviews fixes instead of writing them.
            </p>
            <div className="flex flex-wrap items-center gap-3">
              <Link
                to={primary.to}
                className="inline-flex items-center gap-2 rounded-md bg-[#ec7459] px-5 py-2.5 text-sm font-semibold text-[#120a0c] shadow-[0_0_40px_-8px_rgba(236,116,89,0.8)] transition hover:bg-[#f08a73]"
              >
                {primary.label} <ArrowRight className="h-4 w-4" />
              </Link>
              <a
                href="#how-it-works"
                className="rounded-md px-4 py-2.5 text-sm font-medium text-stone-300 ring-1 ring-white/10 hover:text-white hover:ring-white/25"
              >
                How it works
              </a>
            </div>
          </div>
        </div>

        <div className="pointer-events-none absolute bottom-5 right-6 z-10 rounded-full border border-white/10 bg-black/40 px-4 py-1.5 font-mono text-xs text-stone-300 backdrop-blur">
          bugs removed while you watched:{" "}
          <span className="font-semibold text-[#ec7459]" data-testid="squash-count">
            {squashed}
          </span>
        </div>
      </section>

      <section id="how-it-works" className="mx-auto max-w-6xl px-6 py-24">
        <h2 className="text-2xl font-semibold tracking-tight sm:text-3xl">
          From alert to pull request, on its own.
        </h2>
        <p className="mt-3 max-w-2xl text-stone-400">
          Every step is traced, every token is counted, and nothing merges without a human saying
          yes.
        </p>
        <div className="mt-12 grid gap-4 md:grid-cols-3">
          {steps.map(({ icon: Icon, title, body }, i) => (
            <div
              key={title}
              className="relative rounded-xl border border-white/10 bg-gradient-to-b from-white/[0.04] to-transparent p-6"
            >
              <div className="mb-4 flex items-center gap-3">
                <span className="flex h-9 w-9 items-center justify-center rounded-lg bg-orange-300/10 ring-1 ring-orange-300/20">
                  <Icon className="h-4 w-4 text-orange-300" />
                </span>
                <span className="font-mono text-xs text-stone-500">0{i + 1}</span>
              </div>
              <h3 className="font-semibold">{title}</h3>
              <p className="mt-2 text-sm leading-relaxed text-stone-400">{body}</p>
            </div>
          ))}
        </div>
      </section>

      <section className="border-t border-white/5">
        <div className="mx-auto flex max-w-6xl flex-col items-start justify-between gap-6 px-6 py-16 sm:flex-row sm:items-center">
          <div>
            <h2 className="text-xl font-semibold">Point it at a repo and watch.</h2>
            <p className="mt-1 text-sm text-stone-400">
              Connect GitHub, pick your models, and send your first alert.
            </p>
          </div>
          <Link
            to={primary.to}
            className="inline-flex items-center gap-2 rounded-md bg-[#f3e6e2] px-5 py-2.5 text-sm font-semibold text-[#120a0c] hover:bg-[#ebd9d6]"
          >
            {primary.label} <ArrowRight className="h-4 w-4" />
          </Link>
        </div>
      </section>
    </div>
  );
}
