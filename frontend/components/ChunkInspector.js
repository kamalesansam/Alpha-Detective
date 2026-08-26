"use client";

import { useCallback, useEffect, useState } from "react";
import Skeleton from "./Skeleton";
import { getDocumentChunks } from "@/lib/api";
import { describeIngest, plural } from "@/lib/format";

/**
 * Chunk inspector for one document (CONTRACTS §1.8, §4.3).
 *
 * Mounted only while a row is expanded, so the GET is lazy by construction —
 * never on list render. It is a read-only view of what indexing produced: no
 * LLM calls, no embeddings, no re-parsing, and exempt from the per-IP throttle.
 *
 * A backend that predates v1.2 has no such route; any failure degrades to a
 * quiet "not available" line with a retry, never a crash and never an error
 * banner — the document list itself is still perfectly usable.
 *
 * Previews are document-derived text and render as React text (§4.1).
 */

const HEAD = "text-[11px] font-semibold uppercase tracking-[0.06em] text-text-3";
const GRID = "grid grid-cols-[44px_36px_52px_54px_minmax(0,1fr)] items-center gap-x-3";

const LOADING = { status: "loading", chunks: [], error: null };

export default function ChunkInspector({ doc }) {
  const [state, setState] = useState(LOADING);
  // `attempt` re-runs the effect on retry; the effect never calls setState
  // synchronously (the loading reset happens in the retry handler).
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    getDocumentChunks(doc.id)
      .then((res) => {
        if (cancelled) return;
        const chunks = Array.isArray(res?.chunks) ? res.chunks : [];
        setState({ status: "ready", chunks, error: null });
      })
      .catch((err) => {
        if (cancelled) return;
        setState({ status: "error", chunks: [], error: err });
      });
    return () => {
      cancelled = true;
    };
  }, [doc.id, attempt]);

  const retry = useCallback(() => {
    setState(LOADING);
    setAttempt((n) => n + 1);
  }, []);

  const withTables = state.chunks.filter((c) => c.has_table).length;

  return (
    <div data-testid="chunk-inspector" data-doc-id={doc.id} className="flex flex-col gap-2.5">
      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1">
        <span className="text-xs text-text-2">
          {[
            describeIngest({ pages: doc.pages, tables: doc.tables, chunks: doc.chunks }),
            doc.tables === 0 ? "no tables" : null,
            doc.pages == null ? "no page map for this format" : null,
          ]
            .filter(Boolean)
            .join(" · ")}
        </span>
        <span className="font-mono text-[11px] text-text-3">id {doc.id}</span>
      </div>

      {state.status === "loading" ? (
        <Skeleton lines={3} />
      ) : state.status === "error" ? (
        <div className="flex flex-wrap items-center gap-2 text-[11px] text-text-3">
          <span>
            {state.error?.code === "unauthorized"
              ? "Access code required to read the chunk inventory."
              : "Chunk inventory is not available from this backend."}
          </span>
          <button
            type="button"
            onClick={retry}
            className="inline-flex h-7 items-center rounded-control border border-border bg-surface px-2.5 text-xs font-medium text-accent hover:border-border-strong hover:text-accent-hover"
          >
            Retry
          </button>
        </div>
      ) : state.chunks.length === 0 ? (
        <div className="text-[11px] text-text-3">No chunks recorded for this document.</div>
      ) : (
        <div className="overflow-hidden rounded-control border border-border bg-surface">
          <div className={`${GRID} h-6 border-b border-border px-3 ${HEAD}`}>
            <span>Chunk</span>
            <span>p.</span>
            <span className="text-right">Chars</span>
            <span>Kind</span>
            <span>Preview</span>
          </div>
          {state.chunks.map((c, i) => (
            <div
              key={c.chunk_ix ?? i}
              data-testid="chunk-row"
              className={`${GRID} h-7 px-3 ${i % 2 === 1 ? "bg-bg" : ""}`}
            >
              <span className="font-mono text-[11px] text-text-2">{c.chunk_ix}</span>
              <span className="font-mono text-[11px] text-text-2">
                {c.page == null ? "—" : c.page}
              </span>
              <span className="text-right font-mono text-[11px] text-text">{c.chars}</span>
              <span>
                {c.has_table ? (
                  <span
                    data-testid="chunk-table-badge"
                    className="inline-flex h-[18px] items-center rounded-[4px] border border-border bg-bg px-1.5 font-mono text-[11px] font-semibold text-text-2"
                  >
                    TABLE
                  </span>
                ) : (
                  <span className="font-mono text-[11px] text-text-3">text</span>
                )}
              </span>
              <span className="truncate font-mono text-[11px] text-text-3" title={c.preview}>
                {c.preview}
              </span>
            </div>
          ))}
          <div className="flex h-7 items-center justify-between border-t border-border px-3">
            <span className="text-[11px] text-text-3">
              {plural(state.chunks.length, "indexed chunk")}
            </span>
            <span className="font-mono text-[11px] text-text-3">
              {withTables > 0 ? `${withTables} with table content` : "no table content"}
            </span>
          </div>
        </div>
      )}
    </div>
  );
}
