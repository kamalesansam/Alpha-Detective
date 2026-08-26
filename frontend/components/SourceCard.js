"use client";

import { useState } from "react";
import { ChevronDown } from "lucide-react";
import { plural } from "@/lib/format";

/**
 * One retrieved source: left accent rule, doc / p.N / score header, mono
 * snippet clamped to two lines with a real "Show full passage" expander
 * (reveals the full retrieved snippet window). `docChunks` is the document's
 * total chunk count from GET /api/documents (the API does not expose a chunk
 * index, so position is reported as the doc's chunk total).
 * The 2px --accent left rule (canvas; design round1 MAJOR-1) is an inline
 * style so it can never lose to utility emission order in dev-mode CSS.
 */
export default function SourceCard({ citation, highlighted, docChunks }) {
  const [expanded, setExpanded] = useState(false);
  const c = citation;
  const score = typeof c.score === "number" ? c.score.toFixed(2) : null;

  return (
    <article
      data-testid="source-card"
      data-n={c.n}
      className={`flex flex-col gap-2.5 rounded-card border border-border bg-surface p-4 shadow-card ${
        highlighted ? "outline outline-2 outline-offset-2 outline-accent" : ""
      }`}
      style={{ borderLeft: "2px solid var(--accent)" }}
    >
      <div className="flex items-center gap-2">
        <span
          className="inline-flex h-5 min-w-5 shrink-0 items-center justify-center rounded-control bg-accent-soft px-1.5 font-mono text-[11px] font-semibold text-accent"
          aria-hidden="true"
        >
          {c.n}
        </span>
        <span className="min-w-0 truncate text-[13px] font-medium text-text">{c.doc_name}</span>
        {c.page != null ? (
          <span className="shrink-0 text-xs text-text-3">p.{c.page}</span>
        ) : null}
        {score != null ? (
          <span className="ml-auto shrink-0 font-mono text-xs text-text-3">{score}</span>
        ) : null}
      </div>

      <p
        className={`font-mono text-xs leading-relaxed text-text-2 ${expanded ? "" : "line-clamp-2"}`}
      >
        {c.snippet}
      </p>

      <div className="flex items-center justify-between">
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          aria-expanded={expanded}
          className="inline-flex items-center gap-1 text-xs font-medium text-accent hover:text-accent-hover"
        >
          <span>{expanded ? "Hide full passage" : "Show full passage"}</span>
          <ChevronDown
            size={12}
            strokeWidth={1.5}
            className={`shrink-0 transition-transform ${expanded ? "rotate-180" : ""}`}
            aria-hidden="true"
          />
        </button>
        {docChunks != null ? (
          <span className="font-mono text-[11px] text-text-3">{plural(docChunks, "chunk")} in doc</span>
        ) : null}
      </div>
    </article>
  );
}
