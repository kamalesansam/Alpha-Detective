"use client";

import { useState } from "react";
import { ArrowRight, Loader2 } from "lucide-react";

function ScopeChip({ active, onClick, docId, label, count }) {
  return (
    <button
      type="button"
      data-testid="scope-chip"
      data-doc-id={docId}
      onClick={onClick}
      aria-pressed={active}
      className={`inline-flex h-7 shrink-0 items-center gap-1.5 rounded-control border px-3 text-xs font-medium ${
        active
          ? "border-accent bg-accent-soft text-accent"
          : "border-border bg-surface text-text-2 hover:border-border-strong hover:text-text"
      }`}
    >
      <span className="max-w-64 truncate">{label}</span>
      {count > 0 ? (
        <span className={`font-mono text-[11px] ${active ? "" : "text-text-3"}`}>{count}</span>
      ) : null}
    </button>
  );
}

/**
 * "Explain retrieval" switch (CONTRACTS §1.9 / §4.3). Flat track + knob on the
 * existing tokens — no new colour, no gradient. Off by default; the state
 * lives in the page's memory only, never localStorage.
 */
function ExplainSwitch({ checked, onChange }) {
  return (
    <button
      type="button"
      data-testid="explain-toggle"
      role="switch"
      aria-checked={checked}
      onClick={() => onChange(!checked)}
      title="Show the retrieval funnel under each answer. Costs no extra AI calls."
      className={`inline-flex h-7 shrink-0 items-center gap-2 rounded-control border px-2.5 text-xs font-medium ${
        checked
          ? "border-accent bg-accent-soft text-accent"
          : "border-border bg-surface text-text-2 hover:border-border-strong hover:text-text"
      }`}
    >
      <span
        aria-hidden="true"
        className={`relative h-3.5 w-6 shrink-0 rounded-full ${checked ? "bg-accent" : "bg-border-strong"}`}
      >
        <span
          className={`absolute top-0.5 h-2.5 w-2.5 rounded-full bg-surface transition-[left] ${
            checked ? "left-3" : "left-0.5"
          }`}
        />
      </span>
      <span>Explain retrieval</span>
    </button>
  );
}

/**
 * Pinned composer: question input + Ask, document-scope multiselect chips with
 * chunk counts ("All documents" default), suggested questions while the thread
 * is empty. Enter submits; onAsk(question, docIds) with [] meaning all.
 */
export default function AskPanel({
  documents,
  busy,
  onAsk,
  initialQuestion = "",
  suggestions = [],
  showSuggestions = false,
  explain = false,
  onExplainChange,
}) {
  const [question, setQuestion] = useState(initialQuestion);
  const [selected, setSelected] = useState([]); // doc ids; empty = all documents
  const [lastInitial, setLastInitial] = useState(initialQuestion);

  // ?q= prefill arrives after mount (the page reads window.location in an
  // effect) — sync it via render-time state adjustment, not an effect.
  if (initialQuestion !== lastInitial) {
    setLastInitial(initialQuestion);
    if (initialQuestion) setQuestion(initialQuestion);
  }

  const totalChunks = documents.reduce((sum, d) => sum + (d.chunks || 0), 0);

  function toggleDoc(id) {
    setSelected((prev) =>
      prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]
    );
  }

  function submit(e) {
    e.preventDefault();
    const q = question.trim();
    if (!q || busy) return;
    onAsk(q, selected);
  }

  function askSuggestion(s) {
    if (busy) return;
    setQuestion(s);
    onAsk(s, selected);
  }

  return (
    <div className="flex flex-col gap-3 rounded-card border border-border bg-surface px-5 py-4 shadow-card">
      <form onSubmit={submit} className="flex gap-3">
        <input
          data-testid="question-input"
          data-cmdk-target
          type="text"
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder="Ask a question about your documents…"
          aria-label="Ask a question about your documents"
          disabled={busy}
          className="h-10 min-w-0 flex-1 rounded-control border border-border-strong bg-surface px-3 text-sm text-text placeholder:text-text-3 disabled:opacity-60"
        />
        <button
          data-testid="question-submit"
          type="submit"
          disabled={busy || !question.trim()}
          className="inline-flex h-10 shrink-0 items-center gap-2 rounded-control bg-accent px-4 text-sm font-medium text-surface hover:bg-accent-hover disabled:cursor-not-allowed disabled:opacity-50"
        >
          {busy ? (
            <Loader2 size={16} strokeWidth={1.5} className="animate-spin" aria-hidden="true" />
          ) : null}
          <span>Ask</span>
          {!busy ? <ArrowRight size={16} strokeWidth={1.5} aria-hidden="true" /> : null}
        </button>
      </form>

      <div className="flex items-center gap-2">
        <div
          className={`flex min-w-0 flex-1 items-center gap-2 ${
            showSuggestions ? "flex-wrap" : "flex-nowrap overflow-x-auto pb-0.5"
          }`}
          role="group"
          aria-label="Document scope"
        >
          <ScopeChip
            active={selected.length === 0}
            onClick={() => setSelected([])}
            docId="all"
            label="All documents"
            count={totalChunks}
          />
          {documents.map((d) => (
            <ScopeChip
              key={d.id}
              active={selected.includes(d.id)}
              onClick={() => toggleDoc(d.id)}
              docId={d.id}
              label={d.name}
              count={d.chunks}
            />
          ))}
        </div>
        {onExplainChange ? <ExplainSwitch checked={explain} onChange={onExplainChange} /> : null}
      </div>

      {showSuggestions && suggestions.length > 0 ? (
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-[11px] font-semibold uppercase tracking-[0.06em] text-text-3">
            Suggested
          </span>
          {suggestions.map((s) => (
            <button
              key={s}
              type="button"
              data-testid="suggested-question"
              onClick={() => askSuggestion(s)}
              className="inline-flex h-[26px] items-center rounded-control border border-border bg-surface px-2.5 text-xs text-text-2 hover:border-border-strong hover:text-text"
            >
              {s}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}
