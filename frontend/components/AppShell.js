"use client";

import { createContext, useCallback, useContext, useEffect, useState } from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { FileText, LayoutDashboard, MessageSquare, RefreshCw } from "lucide-react";
import StatusPill from "./StatusPill";
import ErrorBanner from "./ErrorBanner";
import { useHealth } from "./useHealth";

const AppContext = createContext({
  health: null,
  offline: false,
  refreshKey: 0,
  requestRefresh: () => {},
});

/** Shared app state: health poller + manual-refresh signal pages refetch on. */
export function useApp() {
  return useContext(AppContext);
}

const NAV = [
  { href: "/", label: "Overview", icon: LayoutDashboard, testId: "nav-overview" },
  { href: "/documents", label: "Documents", icon: FileText, testId: "nav-documents" },
  { href: "/ask", label: "Ask", icon: MessageSquare, testId: "nav-ask" },
];

const TITLES = { "/": "Overview", "/documents": "Documents", "/ask": "Ask" };

// ⌘K focuses the page's primary ask input, or jumps to /ask.
function focusAsk(router) {
  const el = document.querySelector("[data-cmdk-target]");
  if (el) el.focus();
  else router.push("/ask");
}

export default function AppShell({ children }) {
  const pathname = usePathname();
  const router = useRouter();
  const { health, offline, refresh } = useHealth();
  const [refreshKey, setRefreshKey] = useState(0);

  const requestRefresh = useCallback(() => {
    setRefreshKey((k) => k + 1);
    refresh();
  }, [refresh]);

  useEffect(() => {
    function onKey(e) {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        focusAsk(router);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [router]);

  const title = TITLES[pathname] ?? "Alpha Detective";
  const docCount = health?.documents;
  const checking = !offline && !health;

  return (
    <AppContext.Provider value={{ health, offline, refreshKey, requestRefresh }}>
      <div className="flex h-screen bg-bg">
        <aside className="flex w-60 shrink-0 flex-col border-r border-border bg-surface">
          <div className="flex h-14 shrink-0 items-center px-5">
            <span className="text-[15px] font-semibold text-text">Alpha Detective</span>
          </div>
          <nav className="flex flex-col gap-0.5 px-3 py-2" aria-label="Main navigation">
            {NAV.map(({ href, label, icon: Icon, testId }) => {
              const active = pathname === href;
              return (
                <Link
                  key={href}
                  href={href}
                  data-testid={testId}
                  aria-current={active ? "page" : undefined}
                  className={`flex h-9 items-center gap-2.5 rounded-control px-2.5 text-sm font-medium ${
                    active ? "bg-accent-soft text-accent" : "text-text-2 hover:bg-bg hover:text-text"
                  }`}
                >
                  <Icon size={16} strokeWidth={1.5} className="shrink-0" aria-hidden="true" />
                  <span>{label}</span>
                  {/* badge hidden at zero — mono "0" reads as "8" at 11px
                      (design round1 MINOR-3) */}
                  {href === "/documents" && docCount != null && docCount > 0 ? (
                    <span
                      className={`ml-auto font-mono text-[11px] ${active ? "text-accent" : "text-text-3"}`}
                    >
                      {docCount}
                    </span>
                  ) : null}
                </Link>
              );
            })}
          </nav>
          <div className="grow" />
          <div className="p-4">
            <StatusPill health={health} offline={offline} />
          </div>
        </aside>

        <div className="flex min-w-0 grow flex-col">
          <header className="flex h-14 shrink-0 items-center justify-between border-b border-border bg-surface px-6">
            <h1 className="text-xl font-semibold text-text">{title}</h1>
            <div className="flex items-center gap-3">
              <button
                type="button"
                onClick={() => focusAsk(router)}
                aria-label="Focus the ask input (Cmd+K)"
                className="inline-flex h-[22px] items-center rounded-control border border-border bg-bg px-[7px] font-mono text-[11px] text-text-3 hover:text-text-2"
              >
                ⌘K
              </button>
              <button
                type="button"
                onClick={requestRefresh}
                aria-label="Refresh data"
                className="inline-flex h-8 w-8 items-center justify-center rounded-control border border-border bg-surface text-text-2 hover:text-text"
              >
                <RefreshCw size={16} strokeWidth={1.5} aria-hidden="true" />
              </button>
              <div className="flex items-center gap-2">
                <span
                  data-testid="health-dot"
                  className={`h-2 w-2 rounded-full ${
                    offline ? "bg-negative" : checking ? "bg-border-strong" : "bg-positive"
                  }`}
                  aria-hidden="true"
                />
                <span className="text-xs text-text-3">
                  {offline ? "Backend offline" : checking ? "Checking…" : "Backend healthy"}
                </span>
              </div>
            </div>
          </header>

          {/* Top spacing lives on the inner (scrolling) wrapper, not the scroll
              container: a padding-top on <main> is a band scrolled content
              stays visible in, letting thread content peek above /ask's pinned
              sticky composer (design round1 MINOR-4). With pt on the inner
              div, a sticky child pins flush at the clip edge — nothing can
              render above it. */}
          <main className="min-w-0 grow overflow-y-auto px-6 pb-6">
            <div className="mx-auto flex max-w-[1120px] flex-col gap-6 pt-6">
              {offline ? (
                <ErrorBanner message="Backend offline — run `make dev` to start the API. This page reconnects automatically" />
              ) : null}
              {children}
            </div>
          </main>
        </div>
      </div>
    </AppContext.Provider>
  );
}
