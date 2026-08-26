"use client";

import { useEffect, useRef, useState } from "react";
import { Info } from "lucide-react";
import CitationChip from "./CitationChip";
import SourceCard from "./SourceCard";
import Skeleton from "./Skeleton";
import ErrorBanner from "./ErrorBanner";
import { formatMs, plural } from "@/lib/format";

const LABEL = "text-[11px] font-semibold uppercase tracking-[0.06em] text-text-3";

// Parses literal [n] markers (CONTRACTS §1.6) into clickable chips — but only
// for n in the answer's actual citation list. Document text can carry literal
// [n] markers (extractive answers embed snippets verbatim), and those must
// render as plain text, never as citation affordances (security round2 m1).
function AnswerText({ answer, onChip, validNs }) {
  const parts = String(answer).split(/(\[\d+\])/g);
  return (
    <div data-testid="answer-text" className="whitespace-pre-line text-sm leading-[1.65] text-text">
      {parts.map((part, i) => {
        const m = /^\[(\d+)\]$/.exec(part);
        return m && validNs.has(Number(m[1])) ? (
          <CitationChip key={i} n={Number(m[1])} onClick={onChip} />
        ) : (
          <span key={i}>{part}</span>
        );
      })}
    </div>
  );
}

function errorProps(error, onRetry) {
  if (error.code === "rate_limited") {
    return {
      message: "Free-tier rate limit hit",
      retryAfterS: error.retryAfterS ?? 30,
      onRetry,
    };
  }
  if (error.code === "offline") {
    return { message: "Backend offline — run `make dev` to start the API", onRetry };
  }
  return { message: error.message || "Request failed", onRetry };
}

/**
 * One thread entry on /ask: question header + meta line, then (by state)
 * skeleton, ErrorBanner, neutral no-answer card, or the answer card with mode
 * badge, inline citation chips, timings footer, and the SOURCES grid.
 * Chip click scrolls to and briefly highlights the matching SourceCard.
 */
export default function AnswerCard({ question, result, error, meta, onRetry, chunksByDocId }) {
  const [highlightN, setHighlightN] = useState(null);
  const sourceRefs = useRef({});
  const timerRef = useRef(null);

  useEffect(() => () => clearTimeout(timerRef.current), []);

  function goToSource(n) {
    const el = sourceRefs.current[n];
    if (!el) return;
    el.scrollIntoView({ behavior: "smooth", block: "center" });
    setHighlightN(n);
    clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => setHighlightN(null), 1600);
  }

  const citations = result?.citations ?? [];
  const generative = result?.mode === "generative";
  const t = result?.timings;

  return (
    <section className="flex flex-col gap-2">
      <div className="flex flex-col gap-1">
        <h2 className="text-base font-semibold text-text">{question}</h2>
        <div className="text-xs text-text-3">
          Asked {meta.askedClock} · {meta.scopeLabel}
          {result && !result.no_answer ? ` · ${plural(citations.length, "chunk")} retrieved` : ""}
        </div>
      </div>

      {error ? (
        <ErrorBanner {...errorProps(error, onRetry)} />
      ) : !result ? (
        <div className="rounded-card border border-border bg-surface p-4 shadow-card">
          <Skeleton lines={3} />
        </div>
      ) : result.no_answer ? (
        <div
          data-testid="no-answer-card"
          className="rounded-card border border-border bg-surface px-4 py-3.5 text-sm leading-normal text-text-2 shadow-card"
        >
          {result.answer}
        </div>
      ) : (
        <>
          <div
            data-testid="answer-card"
            className="flex flex-col gap-2.5 rounded-card border border-border bg-surface p-5 shadow-card"
          >
            <div className="flex items-center justify-between gap-4">
              <div className="flex items-center gap-2">
                <span className={LABEL}>Answer</span>
                <span
                  data-testid="mode-badge"
                  className={`inline-flex h-5 items-center rounded-control px-2 text-[11px] font-semibold tracking-[0.06em] ${
                    generative ? "bg-accent-soft text-accent" : "bg-warning-soft text-warning"
                  }`}
                >
                  {String(result.mode).toUpperCase()}
                </span>
              </div>
              <span className="truncate font-mono text-[11px] text-text-3">
                {generative ? result.model : "matched excerpts · no LLM"}
              </span>
            </div>

            {!generative ? (
              <div className="flex items-start gap-2 rounded-control border border-warning/20 bg-warning-soft px-3 py-2.5">
                <Info size={14} strokeWidth={1.5} className="mt-0.5 shrink-0 text-warning" aria-hidden="true" />
                <div className="text-[13px] leading-normal text-warning">
                  Retrieval-only mode — no API key configured. Showing matched excerpts.
                </div>
              </div>
            ) : null}

            <AnswerText
              answer={result.answer}
              onChip={goToSource}
              validNs={new Set(citations.map((c) => c.n))}
            />

            <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 border-t border-border pt-2.5">
              <div className="flex flex-wrap items-center gap-2">
                {citations.map((c) => (
                  <button
                    key={c.n}
                    type="button"
                    onClick={() => goToSource(c.n)}
                    aria-label={`Go to source ${c.n}`}
                    className="inline-flex h-[22px] items-center rounded-control bg-accent-soft px-2 font-mono text-[11px] font-medium text-accent hover:bg-accent hover:text-surface"
                  >
                    {c.n}
                    {c.page != null ? ` · p.${c.page}` : ""}
                    {typeof c.score === "number" ? ` · ${c.score.toFixed(2)}` : ""}
                  </button>
                ))}
              </div>
              {t ? (
                <span className="whitespace-nowrap font-mono text-[11px] text-text-3">
                  retrieval {formatMs(t.retrieval_ms)} · rerank {formatMs(t.rerank_ms)} · llm{" "}
                  {formatMs(t.llm_ms)} · total {formatMs(t.total_ms)}
                </span>
              ) : null}
            </div>
          </div>

          {citations.length > 0 ? (
            <div className="mt-2 flex flex-col gap-2">
              <div className={LABEL}>Sources</div>
              <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                {citations.map((c) => (
                  <div
                    key={c.n}
                    ref={(el) => {
                      sourceRefs.current[c.n] = el;
                    }}
                  >
                    <SourceCard
                      citation={c}
                      highlighted={highlightN === c.n}
                      docChunks={chunksByDocId ? chunksByDocId[c.doc_id] : null}
                    />
                  </div>
                ))}
              </div>
            </div>
          ) : null}
        </>
      )}
    </section>
  );
}
