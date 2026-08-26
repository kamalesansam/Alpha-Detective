"use client";

import { useState } from "react";
import { ChevronDown, Loader2, Trash2 } from "lucide-react";
import ChunkInspector from "./ChunkInspector";
import { extLabel, formatBytes, formatDateTime, plural } from "@/lib/format";

// Tables sits next to Pages: both are "what the parser found", both are
// per-doc manifest fields (§1.4). The column only exists when the backend
// actually reports the field — otherwise it is dead space (design r3 m-4).
const GRID_WITH_TABLES =
  "grid grid-cols-[minmax(0,1fr)_76px_90px_66px_66px_76px_86px_136px_112px] items-center gap-x-3 px-5";
const GRID_NO_TABLES =
  "grid grid-cols-[minmax(0,1fr)_76px_90px_66px_76px_86px_136px_112px] items-center gap-x-3 px-5";
const HEAD = "text-[11px] font-semibold uppercase tracking-[0.06em] text-text-3";

/**
 * Documents table per the canvas: zebra rows, 40px height, sortable Uploaded
 * column, hover actions (View chunks expander + delete with plain confirm),
 * footer totals. "View chunks" expands the row into the §1.8 chunk inspector,
 * which fetches lazily on expand — never on list render.
 */
export default function DocumentsTable({ documents, onDelete, busyId }) {
  const [sortAsc, setSortAsc] = useState(false);
  const [expandedId, setExpandedId] = useState(null);

  const rows = [...documents].sort((a, b) =>
    sortAsc
      ? String(a.uploaded_at).localeCompare(String(b.uploaded_at))
      : String(b.uploaded_at).localeCompare(String(a.uploaded_at))
  );
  const totalPages = rows.reduce((s, d) => s + (d.pages || 0), 0);
  const totalChunks = rows.reduce((s, d) => s + (d.chunks || 0), 0);
  const totalBytes = rows.reduce((s, d) => s + (d.size_bytes || 0), 0);
  // A pre-v1.2 backend sends no `tables` at all — report nothing rather than 0.
  const hasTables = rows.some((d) => Number.isFinite(d.tables));
  const totalTables = rows.reduce((s, d) => s + (Number.isFinite(d.tables) ? d.tables : 0), 0);
  const GRID = hasTables ? GRID_WITH_TABLES : GRID_NO_TABLES;

  return (
    <div
      role="table"
      aria-label="Indexed documents"
      className="overflow-hidden rounded-card border border-border bg-surface shadow-card"
    >
      <div role="row" className={`${GRID} h-10 border-b border-border bg-bg`}>
        <div role="columnheader" className={HEAD}>Name</div>
        <div role="columnheader" className={HEAD}>Type</div>
        <div role="columnheader" className={HEAD}>Status</div>
        <div role="columnheader" className={`${HEAD} text-right`}>Pages</div>
        {hasTables ? (
          <div role="columnheader" className={`${HEAD} text-right`}>Tables</div>
        ) : null}
        <div role="columnheader" className={`${HEAD} text-right`}>Chunks</div>
        <div role="columnheader" className={`${HEAD} text-right`}>Size</div>
        <div role="columnheader" aria-sort={sortAsc ? "ascending" : "descending"}>
          <button
            type="button"
            onClick={() => setSortAsc((v) => !v)}
            aria-label={`Sort by upload date, currently ${sortAsc ? "oldest" : "newest"} first`}
            className="flex items-center gap-1 text-[11px] font-semibold uppercase tracking-[0.06em] text-text-2"
          >
            <span>Uploaded</span>
            <ChevronDown
              size={12}
              strokeWidth={1.5}
              className={`shrink-0 transition-transform ${sortAsc ? "rotate-180" : ""}`}
              aria-hidden="true"
            />
          </button>
        </div>
        <div role="columnheader" aria-label="Actions" />
      </div>

      {rows.map((d, i) => {
        const busy = busyId === d.id;
        const expanded = expandedId === d.id;
        return (
          <div key={d.id}>
            <div
              data-testid="doc-row"
              data-doc-name={d.name}
              role="row"
              className={`${GRID} group h-10 border-b border-border ${i % 2 === 1 ? "bg-bg" : ""} ${
                busy ? "opacity-60" : ""
              }`}
            >
              <div role="cell" className="truncate text-[13px] font-medium text-text">{d.name}</div>
              <div role="cell">
                <span className="inline-flex h-[18px] items-center rounded-control border border-border bg-bg px-1.5 text-[11px] font-semibold text-text-2">
                  {extLabel(d.ext)}
                </span>
              </div>
              <div role="cell">
                <span className="inline-flex h-5 items-center rounded-control bg-positive-soft px-2 text-[11px] font-semibold text-positive">
                  Indexed
                </span>
              </div>
              <div role="cell" className={`text-right font-mono text-xs ${d.pages != null ? "text-text" : "text-text-3"}`}>
                {d.pages != null ? d.pages : "—"}
              </div>
              {hasTables ? (
                <div
                  role="cell"
                  data-testid="doc-tables"
                  className={`text-right font-mono text-xs ${d.tables ? "text-text" : "text-text-3"}`}
                >
                  {Number.isFinite(d.tables) ? d.tables : "—"}
                </div>
              ) : null}
              <div role="cell" className="text-right font-mono text-xs text-text">{d.chunks}</div>
              <div role="cell" className="text-right font-mono text-xs text-text">{formatBytes(d.size_bytes)}</div>
              {/* font-sans pinned explicitly: dates are Inter, mono is for figures
                  (design round1 MINOR-1 — immune to any inherited/dev-CSS state) */}
              <div role="cell" className="font-sans text-[13px] text-text-2">{formatDateTime(d.uploaded_at)}</div>
              <div
                role="cell"
                className="flex items-center justify-end gap-3 opacity-0 transition-opacity focus-within:opacity-100 group-hover:opacity-100"
              >
                <button
                  type="button"
                  onClick={() => setExpandedId(expanded ? null : d.id)}
                  aria-expanded={expanded}
                  className="whitespace-nowrap text-xs font-medium text-accent hover:text-accent-hover"
                >
                  {expanded ? "Hide chunks" : "View chunks"}
                </button>
                <button
                  type="button"
                  data-testid="doc-delete"
                  onClick={() => onDelete(d.id)}
                  disabled={busy}
                  aria-label={`Delete ${d.name}`}
                  className="inline-flex text-text-3 hover:text-negative disabled:cursor-not-allowed"
                >
                  {busy ? (
                    <Loader2 size={16} strokeWidth={1.5} className="animate-spin" aria-hidden="true" />
                  ) : (
                    <Trash2 size={16} strokeWidth={1.5} aria-hidden="true" />
                  )}
                </button>
              </div>
            </div>

            {expanded ? (
              <div role="row" className="border-b border-border bg-bg px-5 py-3">
                <ChunkInspector doc={d} />
              </div>
            ) : null}
          </div>
        );
      })}

      <div className="flex h-10 items-center justify-between px-5">
        <span className="text-[13px] text-text-2">{plural(rows.length, "document")}</span>
        <span className="font-mono text-xs text-text-3">
          {plural(totalPages, "page")}
          {hasTables ? ` · ${plural(totalTables, "table")}` : ""} · {plural(totalChunks, "chunk")} ·{" "}
          {formatBytes(totalBytes)}
        </span>
      </div>
    </div>
  );
}
