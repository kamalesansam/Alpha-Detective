"use client";

import { ChevronDown } from "lucide-react";

/**
 * PipelineInspector — the retrieval funnel (CONTRACTS §1.9, §4.2).
 *
 * An analyst's funnel, not a dashboard: one dense table per stage, in the
 * order the pipeline executed them, so you can read a chunk's journey from
 * candidate to citation. No charts, no gradients, no decoration, no new
 * design tokens, and no status colours — rank movement and pass/fail are
 * carried by mono type, not by hue.
 *
 * Stage presence is data, not configuration (§1.9.3, resolution 7):
 *   · `dense`  is absent entirely in keyless mode
 *   · `rerank` is absent entirely when reranking is effectively off
 *   · `fusion` is always present but may be method:"passthrough"
 *   · empty corpus ⇒ `guardrail` alone, with nonempty:"fail"
 * Everything renders off `stages`, so any combination — and any stage name
 * added later — degrades to something readable instead of crashing.
 *
 * Every string here is document-derived and rendered as React text (§4.1).
 */

const LABEL = "text-[11px] font-semibold uppercase tracking-[0.06em] text-text-3";
const HEAD = "text-[11px] font-semibold uppercase tracking-[0.06em] text-text-3";
const CELL = "font-mono text-[11px] text-text-2";

// Column templates. The first column is a constant 72px well in every stage
// (design r3 M-4) so Document / p. / Chunk share one x-position down the whole
// funnel; rerank's `#7 → #1` is the widest thing it must hold. Snippet takes
// the slack; figures stay fixed so numerals line up (tabular, §8).
const RANK_COL = "72px";
const COLS = {
  base: `grid grid-cols-[${RANK_COL}_minmax(96px,150px)_34px_42px_minmax(0,1fr)_58px] items-center gap-x-3 px-4`,
  fusion: `grid grid-cols-[${RANK_COL}_minmax(96px,150px)_34px_42px_46px_46px_minmax(0,1fr)_58px] items-center gap-x-3 px-4`,
};
COLS.rerank = COLS.base;

const TITLES = {
  bm25: "BM25 · sparse",
  dense: "Dense · embeddings",
  fusion: "Fusion",
  rerank: "Rerank · cross-encoder",
  guardrail: "Guardrail",
};

// M-2: `mode` is a backend enum. "none" reads as *not working* — and collides
// with the EXTRACTIVE badge and the "Retrieval-only mode" pill saying the same
// thing three different ways. Speak the app's vocabulary instead.
const MODE_LABEL = { none: "retrieval-only", gemini: "gemini" };

function score(v) {
  return typeof v === "number" && Number.isFinite(v) ? v.toFixed(4) : "—";
}

function rank(v) {
  return typeof v === "number" ? `#${v}` : "—";
}

function page(v) {
  return v == null ? "—" : String(v);
}

function itemKey(it, i) {
  return `${it?.doc_id ?? "?"}-${it?.chunk_ix ?? "?"}-${i}`;
}

/** The named check that stopped the pipeline, or null when nothing stopped. */
function stoppedBy(stages) {
  const g = stages.find((st) => st?.stage === "guardrail");
  if (!g || g.passed) return null;
  const checks = g.checks && typeof g.checks === "object" ? g.checks : {};
  const failed = Object.keys(checks).find((name) => checks[name] === "fail");
  return failed ? `guardrail stopped: ${failed}` : "guardrail stopped";
}

function Meta({ children, emphasize }) {
  return (
    <span
      className={`shrink-0 font-mono text-[11px] ${
        emphasize ? "font-medium text-text" : "text-text-3"
      }`}
    >
      {children}
    </span>
  );
}

/** Stage frame: label + mono meta on the left/right of a divider. */
function Stage({ title, meta, emphasizeMeta, children }) {
  return (
    <div className="border-t border-border first:border-t-0">
      <div className="flex h-9 items-center justify-between gap-3 px-4">
        <span className={LABEL}>{title}</span>
        {meta ? <Meta emphasize={emphasizeMeta}>{meta}</Meta> : null}
      </div>
      {children}
    </div>
  );
}

function NoItems() {
  return <div className="px-4 pb-3 text-[11px] text-text-3">No items recorded for this stage.</div>;
}

/** bm25 / dense / any unknown item-bearing stage. */
function ItemTable({ items }) {
  if (items.length === 0) return <NoItems />;
  return (
    <div className="pb-1.5">
      <div className={`${COLS.base} h-6 ${HEAD}`}>
        <span className="text-right">Rank</span>
        <span>Document</span>
        <span>p.</span>
        <span className="text-right">Chunk</span>
        <span>Snippet</span>
        <span className="text-right">Score</span>
      </div>
      {items.map((it, i) => (
        <div key={itemKey(it, i)} className={`${COLS.base} h-7 ${i % 2 === 1 ? "bg-bg" : ""}`}>
          <span className={`${CELL} text-right`}>{rank(i + 1)}</span>
          <span className="truncate text-[11px] text-text" title={it.doc_name}>
            {it.doc_name}
          </span>
          <span className={CELL}>{page(it.page)}</span>
          <span className={`${CELL} text-right`}>{it.chunk_ix}</span>
          <span className="truncate font-mono text-[11px] text-text-3">{it.snippet}</span>
          <span className="text-right font-mono text-[11px] text-text">{score(it.score)}</span>
        </div>
      ))}
    </div>
  );
}

/** fusion: adds each retriever's own rank, `—` where it never surfaced. */
function FusionTable({ items }) {
  if (items.length === 0) return <NoItems />;
  return (
    <div className="pb-1.5">
      <div className={`${COLS.fusion} h-6 ${HEAD}`}>
        <span className="text-right">Rank</span>
        <span>Document</span>
        <span>p.</span>
        <span className="text-right">Chunk</span>
        <span className="text-right">BM25</span>
        <span className="text-right">Dense</span>
        <span>Snippet</span>
        <span className="text-right">Score</span>
      </div>
      {items.map((it, i) => (
        <div key={itemKey(it, i)} className={`${COLS.fusion} h-7 ${i % 2 === 1 ? "bg-bg" : ""}`}>
          <span className={`${CELL} text-right`}>{rank(i + 1)}</span>
          <span className="truncate text-[11px] text-text" title={it.doc_name}>
            {it.doc_name}
          </span>
          <span className={CELL}>{page(it.page)}</span>
          <span className={`${CELL} text-right`}>{it.chunk_ix}</span>
          <span
            className={`text-right font-mono text-[11px] ${
              it.bm25_rank == null ? "text-text-3" : "text-text-2"
            }`}
          >
            {rank(it.bm25_rank)}
          </span>
          <span
            className={`text-right font-mono text-[11px] ${
              it.dense_rank == null ? "text-text-3" : "text-text-2"
            }`}
          >
            {rank(it.dense_rank)}
          </span>
          <span className="truncate font-mono text-[11px] text-text-3">{it.snippet}</span>
          <span className="text-right font-mono text-[11px] text-text">{score(it.score)}</span>
        </div>
      ))}
    </div>
  );
}

/** rerank: the movement column is the point — `#7 → #2`. */
function RerankTable({ items }) {
  if (items.length === 0) return <NoItems />;
  return (
    <div className="pb-1.5">
      <div className={`${COLS.rerank} h-6 ${HEAD}`}>
        <span className="text-right">Move</span>
        <span>Document</span>
        <span>p.</span>
        <span className="text-right">Chunk</span>
        <span>Snippet</span>
        <span className="text-right">Score</span>
      </div>
      {items.map((it, i) => (
        <div key={itemKey(it, i)} className={`${COLS.rerank} h-7 ${i % 2 === 1 ? "bg-bg" : ""}`}>
          <span
            className="whitespace-nowrap text-right font-mono text-[11px] text-text-3"
            aria-label={`rank ${it.before_rank} to ${it.after_rank}`}
          >
            {rank(it.before_rank)}
            <span aria-hidden="true"> → </span>
            <span className="font-medium text-text">{rank(it.after_rank)}</span>
          </span>
          <span className="truncate text-[11px] text-text" title={it.doc_name}>
            {it.doc_name}
          </span>
          <span className={CELL}>{page(it.page)}</span>
          <span className={`${CELL} text-right`}>{it.chunk_ix}</span>
          <span className="truncate font-mono text-[11px] text-text-3">{it.snippet}</span>
          <span className="text-right font-mono text-[11px] text-text">{score(it.score)}</span>
        </div>
      ))}
    </div>
  );
}

/**
 * guardrail: the checklist, always last. §1.9.4 — only the checks that were
 * actually evaluated are present, in evaluation order, so a short list after
 * a `fail` is correct, not missing data. Neutral rows by contract: no status
 * colours, the verdict is carried by the mono token and its weight.
 */
function GuardrailChecks({ stage }) {
  const checks = stage.checks && typeof stage.checks === "object" ? stage.checks : {};
  const names = Object.keys(checks);
  return (
    <div className="pb-2">
      {names.length === 0 ? (
        <div className="px-4 pb-1 text-[11px] text-text-3">No checks recorded.</div>
      ) : (
        names.map((name, i) => {
          const failed = checks[name] === "fail";
          return (
            <div
              key={name}
              className={`flex h-7 items-center justify-between gap-3 px-4 ${
                i % 2 === 1 ? "bg-bg" : ""
              }`}
            >
              <span
                className={`truncate font-mono text-[11px] ${
                  failed ? "font-medium text-text" : "text-text-2"
                }`}
              >
                {name}
              </span>
              <span
                className={`shrink-0 font-mono text-[11px] ${
                  failed ? "font-medium text-text" : "text-text-3"
                }`}
              >
                {failed ? "fail" : "pass"}
              </span>
            </div>
          );
        })
      )}
      {names.length > 0 ? (
        <div className="px-4 pt-1.5 text-[11px] leading-normal text-text-3">
          {stage.passed
            ? "All evaluated checks passed — the answer was allowed through."
            : "The guardrail stopped before the model ran; later checks are not evaluated."}
        </div>
      ) : null}
    </div>
  );
}

function StageBlock({ stage }) {
  const name = String(stage?.stage ?? "");
  const items = Array.isArray(stage?.items) ? stage.items : [];
  const title = TITLES[name] || name || "stage";

  if (name === "guardrail") {
    return (
      <Stage
        title={TITLES.guardrail}
        meta={stage.passed ? "passed" : "stopped"}
        emphasizeMeta={!stage.passed}
      >
        <GuardrailChecks stage={stage} />
      </Stage>
    );
  }

  const meta = [
    name === "fusion" ? (stage?.method ?? "—") : null,
    stage?.k != null ? `k=${stage.k}` : null,
    // Suppressed at zero (round-1 MINOR-3): mono `0` reads as an 8 at 11px,
    // and the empty-stage sentence below already says it in words.
    items.length > 0 ? `${items.length} shown` : null,
    name === "rerank" && stage?.model ? stage.model : null,
  ]
    .filter(Boolean)
    .join(" · ");

  if (name === "fusion") {
    return (
      <Stage title={title} meta={meta}>
        <FusionTable items={items} />
      </Stage>
    );
  }
  if (name === "rerank") {
    return (
      <Stage title={title} meta={meta}>
        <RerankTable items={items} />
      </Stage>
    );
  }
  return (
    <Stage title={title} meta={meta}>
      <ItemTable items={items} />
    </Stage>
  );
}

export default function PipelineInspector({ pipeline, open, onToggle }) {
  const stages = Array.isArray(pipeline?.stages) ? pipeline.stages : [];
  const flow = stages.map((s) => String(s?.stage ?? "?")).join(" → ");
  const summary = pipeline
    ? [
        // M-2: the app's vocabulary, not the raw enum. Unknown future values
        // pass through verbatim rather than being silently relabelled.
        pipeline.mode ? `mode ${MODE_LABEL[pipeline.mode] ?? pipeline.mode}` : null,
        pipeline.rerank ? `rerank ${pipeline.rerank}` : null,
        pipeline.top_k != null ? `top_k ${pipeline.top_k}` : null,
      ]
        .filter(Boolean)
        .join(" · ")
    : null;
  // M-3: a refusal's reason must be legible without expanding anything.
  const stopped = stoppedBy(stages);

  return (
    <div
      data-testid="pipeline-inspector"
      className="overflow-hidden rounded-card border border-border bg-surface shadow-card"
    >
      <button
        type="button"
        data-testid="pipeline-toggle"
        onClick={onToggle}
        aria-expanded={open}
        className="flex h-10 w-full items-center justify-between gap-3 px-4 text-left"
      >
        <span className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5">
          <span className={LABEL}>How this was retrieved</span>
          {summary ? <Meta>{summary}</Meta> : null}
          {stopped ? <Meta emphasize>{stopped}</Meta> : null}
        </span>
        <span className="flex shrink-0 items-center gap-2">
          {pipeline ? (
            <span className="hidden font-mono text-[11px] text-text-3 sm:inline">{flow}</span>
          ) : null}
          <ChevronDown
            size={14}
            strokeWidth={1.5}
            className={`shrink-0 text-text-3 transition-transform ${open ? "rotate-180" : ""}`}
            aria-hidden="true"
          />
        </span>
      </button>

      {open ? (
        !pipeline ? (
          // Graceful degradation: explain was asked for, the backend did not
          // send `pipeline` (older build, or the key is simply absent). Never
          // a crash, never an error — just an honest "not available".
          <div className="border-t border-border px-4 py-3 text-[11px] leading-normal text-text-3">
            Retrieval detail is not available for this answer.
          </div>
        ) : stages.length === 0 ? (
          <div className="border-t border-border px-4 py-3 text-[11px] leading-normal text-text-3">
            No stages were recorded for this query.
          </div>
        ) : (
          <div className="border-t border-border">
            {stages.map((stage, i) => (
              <StageBlock key={`${stage?.stage ?? "stage"}-${i}`} stage={stage} />
            ))}
          </div>
        )
      ) : null}
    </div>
  );
}
