"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ArrowRight, FileText } from "lucide-react";
import { useApp } from "@/components/AppShell";
import StatCard from "@/components/StatCard";
import EmptyState from "@/components/EmptyState";
import Skeleton from "@/components/Skeleton";
import { listDocuments } from "@/lib/api";
import { extLabel, formatBytes, formatDateTime, formatDayTime, isToday, plural } from "@/lib/format";

const LABEL = "text-[11px] font-semibold uppercase tracking-[0.06em] text-text-3";
const CARD = "rounded-card border border-border bg-surface shadow-card";

function llmShort(model) {
  if (!model) return "";
  const m = model.toLowerCase();
  if (m.includes("flash")) return "flash";
  if (m.includes("pro")) return "pro";
  return model.replace(/^gemini-/, "");
}

function PipelineRow({ label, children }) {
  return (
    <div className="flex h-[30px] items-center justify-between gap-4">
      <span className="shrink-0 text-xs text-text-3">{label}</span>
      {children}
    </div>
  );
}

function Dot({ tone }) {
  const cls =
    tone === "positive" ? "bg-positive" : tone === "warning" ? "bg-warning" : tone === "negative" ? "bg-negative" : "bg-border-strong";
  return <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${cls}`} aria-hidden="true" />;
}

export default function OverviewPage() {
  const { health, offline, refreshKey } = useApp();
  const router = useRouter();
  const [docs, setDocs] = useState(null); // {documents, totals} | null while loading
  const [loadError, setLoadError] = useState(null);
  const [q, setQ] = useState("");

  useEffect(() => {
    let cancelled = false;
    listDocuments()
      .then((d) => {
        if (!cancelled) {
          setDocs(d);
          setLoadError(null);
        }
      })
      .catch((err) => {
        // Never leave a skeleton promising data whose request already failed.
        if (!cancelled) setLoadError(err);
      });
    return () => {
      cancelled = true;
    };
  }, [refreshKey, offline]);

  const documents = docs?.documents ?? [];
  const totals = docs?.totals ?? null;

  const docsToday = documents.filter((d) => isToday(d.uploaded_at));
  const chunksToday = docsToday.reduce((s, d) => s + (d.chunks || 0), 0);

  const pagesByExt = Object.entries(
    documents.reduce((acc, d) => {
      if (d.pages != null && d.pages > 0) {
        const key = extLabel(d.ext);
        acc[key] = (acc[key] || 0) + d.pages;
      }
      return acc;
    }, {})
  )
    .sort((a, b) => b[1] - a[1])
    .map(([ext, pages]) => `${pages} ${ext}`)
    .join(" · ");

  const provider = health?.provider;
  const providerValue = offline
    ? "—"
    : provider === "gemini"
      ? `Gemini · ${llmShort(health?.llm_model) || "auto"}`
      : provider === "none"
        ? "Retrieval-only"
        : "—";
  const providerHint =
    !offline && provider === "gemini"
      ? "Free tier · 10 req/min"
      : !offline && provider === "none"
        ? "BM25 + local reranker"
        : undefined;

  const lastIndexed = documents.reduce(
    (latest, d) => (latest && latest >= d.uploaded_at ? latest : d.uploaded_at),
    null
  );
  const maxChunks = documents.reduce((m, d) => Math.max(m, d.chunks || 0), 0);
  const byChunks = [...documents].sort((a, b) => (b.chunks || 0) - (a.chunks || 0)).slice(0, 6);

  function submitQuickAsk(e) {
    e.preventDefault();
    const question = q.trim();
    if (question) router.push(`/ask?q=${encodeURIComponent(question)}`);
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard
          testId="stat-documents"
          label="Documents"
          value={totals ? totals.documents : "—"}
          hint={totals ? (docsToday.length > 0 ? `+${docsToday.length} today` : "0 today") : undefined}
          tone={docsToday.length > 0 ? "positive" : "muted"}
        />
        <StatCard
          testId="stat-chunks"
          label="Indexed chunks"
          value={totals ? totals.chunks : "—"}
          hint={totals ? (chunksToday > 0 ? `+${chunksToday} today` : "0 today") : undefined}
          tone={chunksToday > 0 ? "positive" : "muted"}
        />
        <StatCard
          testId="stat-pages"
          label="Pages"
          value={totals ? totals.pages : "—"}
          hint={totals ? pagesByExt || "—" : undefined}
        />
        <StatCard testId="stat-provider" label="Provider" value={providerValue} hint={providerHint} />
      </div>

      {totals && totals.documents === 0 ? (
        <EmptyState
          title="No documents yet"
          message="Upload earnings calls, filings, or reports to build your index."
          actionLabel="Upload documents"
          onAction={() => router.push("/documents")}
        />
      ) : (
        <>
          <div className="grid grid-cols-1 items-start gap-4 lg:grid-cols-[minmax(0,1fr)_400px]">
            <div data-testid="recent-docs" className={`overflow-hidden ${CARD}`}>
              <div className="flex h-12 items-center justify-between border-b border-border px-5">
                <span className={LABEL}>Recent documents</span>
                <Link href="/documents" className="text-[13px] font-medium text-accent hover:text-accent-hover">
                  View all
                </Link>
              </div>
              {!docs && loadError ? (
                <div className="px-5 py-4 text-[13px] text-text-2">Documents could not be loaded.</div>
              ) : !docs ? (
                <div className="p-5">
                  <Skeleton lines={3} />
                </div>
              ) : (
                documents.slice(0, 5).map((d, i, arr) => (
                  <div
                    key={d.id}
                    className={`flex min-h-14 items-center gap-3 px-5 py-2 ${
                      i < arr.length - 1 ? "border-b border-border" : ""
                    }`}
                  >
                    <FileText size={16} strokeWidth={1.5} className="shrink-0 text-text-3" aria-hidden="true" />
                    <div className="flex min-w-0 grow flex-col gap-0.5">
                      <div className="flex min-w-0 items-center gap-2">
                        <span className="truncate text-sm font-medium text-text">{d.name}</span>
                        <span className="inline-flex h-[18px] shrink-0 items-center rounded-control border border-border bg-bg px-1.5 text-[11px] font-semibold text-text-2">
                          {extLabel(d.ext)}
                        </span>
                      </div>
                      <span className="text-xs text-text-3">
                        {d.pages != null ? `${plural(d.pages, "page")} · ` : ""}
                        {formatBytes(d.size_bytes)} · {plural(d.chunks, "chunk")}
                      </span>
                    </div>
                    <div className="flex shrink-0 items-center gap-3">
                      <span className="inline-flex h-5 items-center rounded-control bg-positive-soft px-2 text-[11px] font-semibold text-positive">
                        Indexed
                      </span>
                      <span className="font-sans text-xs text-text-3">{formatDateTime(d.uploaded_at)}</span>
                    </div>
                  </div>
                ))
              )}
            </div>

            <div className={`flex flex-col gap-3 px-5 py-4 ${CARD}`}>
              <span className={LABEL}>Pipeline</span>
              <div className="flex flex-col">
                <PipelineRow label="Provider">
                  {offline ? (
                    <span className="inline-flex items-center gap-1.5"><Dot tone="negative" /><span className="text-xs font-medium text-text">Offline</span></span>
                  ) : provider === "gemini" ? (
                    <span className="inline-flex items-center gap-1.5"><Dot tone="positive" /><span className="text-xs font-medium text-text">Gemini</span></span>
                  ) : provider === "none" ? (
                    <span className="inline-flex items-center gap-1.5"><Dot tone="warning" /><span className="text-xs font-medium text-text">Retrieval-only</span></span>
                  ) : (
                    <span className="text-xs text-text-3">—</span>
                  )}
                </PipelineRow>
                <PipelineRow label="LLM model">
                  <span className="truncate font-mono text-xs text-text">{health?.llm_model ?? "—"}</span>
                </PipelineRow>
                <PipelineRow label="Embeddings">
                  <span className="truncate font-mono text-xs text-text">{health?.embed_model ?? "—"}</span>
                </PipelineRow>
                <PipelineRow label="Reranker">
                  <span className="inline-flex items-center gap-1.5">
                    <Dot tone={health?.rerank === "on" ? "positive" : "muted"} />
                    <span className="font-mono text-xs text-text">{health?.rerank ?? "—"}</span>
                  </span>
                </PipelineRow>
                <PipelineRow label="Last indexed">
                  <span className="font-sans text-xs text-text">{lastIndexed ? formatDayTime(lastIndexed) : "—"}</span>
                </PipelineRow>
              </div>
            </div>
          </div>

          <div className="grid grid-cols-1 items-start gap-4 lg:grid-cols-[minmax(0,1fr)_400px]">
            <div className={`flex flex-col gap-3 p-5 ${CARD}`}>
              <span className={LABEL}>Quick ask</span>
              <form onSubmit={submitQuickAsk} className="flex gap-3">
                <input
                  data-testid="quick-ask-input"
                  data-cmdk-target
                  type="text"
                  value={q}
                  onChange={(e) => setQ(e.target.value)}
                  placeholder="Ask a question about your documents…"
                  aria-label="Ask a question about your documents"
                  className="h-10 min-w-0 flex-1 rounded-control border border-border-strong bg-surface px-3 text-sm text-text placeholder:text-text-3"
                />
                <button
                  data-testid="quick-ask-submit"
                  type="submit"
                  className="inline-flex h-10 shrink-0 items-center gap-2 rounded-control bg-accent px-4 text-sm font-medium text-surface hover:bg-accent-hover"
                >
                  <span>Ask</span>
                  <ArrowRight size={16} strokeWidth={1.5} aria-hidden="true" />
                </button>
              </form>
            </div>

            <div className={`flex flex-col gap-3 px-5 py-4 ${CARD}`}>
              <span className={LABEL}>Chunks per document</span>
              {!docs && loadError ? (
                <div className="text-[13px] text-text-2">Not available.</div>
              ) : !docs ? (
                <Skeleton lines={3} />
              ) : (
                <div className="flex flex-col gap-2.5">
                  {byChunks.map((d) => (
                    <div key={d.id} className="flex items-center gap-2.5">
                      <span className="w-[150px] shrink-0 truncate text-xs text-text-2">{d.name}</span>
                      <span className="h-1.5 grow overflow-hidden rounded-[3px] bg-accent-soft">
                        <span
                          className="block h-1.5 bg-accent"
                          style={{ width: `${maxChunks ? Math.round(((d.chunks || 0) / maxChunks) * 100) : 0}%` }}
                        />
                      </span>
                      <span className="w-8 shrink-0 text-right font-mono text-xs text-text">{d.chunks}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        </>
      )}
    </div>
  );
}
