"use client";

import { useEffect, useRef, useState } from "react";
import { useApp } from "@/components/AppShell";
import AskPanel from "@/components/AskPanel";
import AnswerCard from "@/components/AnswerCard";
import { listDocuments, postQuery } from "@/lib/api";
import { formatClock } from "@/lib/format";

// Approved canvas suggestions (shown while the thread is empty).
const SUGGESTED = [
  "What was Northwind's diluted EPS in Q2 2026?",
  "What is Helios's FY2026 capex guidance?",
  "How did Meridian's ARR trend in Q2?",
];

export default function AskPage() {
  const { offline, refreshKey } = useApp();
  const [documents, setDocuments] = useState([]);
  const [thread, setThread] = useState([]); // newest first
  const [busy, setBusy] = useState(false);
  const [prefill, setPrefill] = useState("");
  const idRef = useRef(0);
  const autoRanRef = useRef(false);

  useEffect(() => {
    let cancelled = false;
    listDocuments()
      .then((d) => {
        if (!cancelled) setDocuments(d.documents || []);
      })
      .catch(() => {
        /* offline banner (AppShell) explains; retried when offline clears */
      });
    return () => {
      cancelled = true;
    };
  }, [refreshKey, offline]);

  function scopeLabelFor(docIds) {
    if (docIds.length === 0) return "All documents";
    if (docIds.length === 1) {
      return documents.find((d) => d.id === docIds[0])?.name ?? "1 document";
    }
    return `${docIds.length} documents`;
  }

  async function ask(question, docIds, reuseId = null) {
    if (busy) return;
    let id = reuseId;
    if (id == null) {
      id = ++idRef.current;
      setThread((t) => [
        {
          id,
          question,
          docIds,
          scopeLabel: scopeLabelFor(docIds),
          askedClock: formatClock(new Date()),
          result: null,
          error: null,
        },
        ...t,
      ]);
    } else {
      setThread((t) => t.map((e) => (e.id === id ? { ...e, result: null, error: null } : e)));
    }
    setBusy(true);
    try {
      const result = await postQuery({ question, docIds });
      setThread((t) => t.map((e) => (e.id === id ? { ...e, result } : e)));
    } catch (error) {
      setThread((t) => t.map((e) => (e.id === id ? { ...e, error } : e)));
    } finally {
      setBusy(false);
    }
  }

  // ?q= prefills the input and auto-submits exactly once on mount.
  useEffect(() => {
    if (autoRanRef.current) return;
    autoRanRef.current = true;
    const q = new URLSearchParams(window.location.search).get("q");
    if (q && q.trim()) {
      setPrefill(q);
      ask(q.trim(), []);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const chunksByDocId = Object.fromEntries(documents.map((d) => [d.id, d.chunks]));

  return (
    <div className="flex flex-col">
      {/* Pins flush at the scrollport clip edge (AppShell keeps the scroll
          container padding-top-free), so scrolled thread content — incl. the
          in-card amber note — can never peek above the pinned composer
          (design round1 MINOR-4 root cause). */}
      <div className="sticky top-0 z-10 bg-bg pb-3">
        <AskPanel
          documents={documents}
          busy={busy}
          onAsk={(q, ids) => ask(q, ids)}
          initialQuestion={prefill}
          suggestions={SUGGESTED}
          showSuggestions={thread.length === 0}
        />
      </div>

      <div className="flex flex-col gap-[18px] pt-1">
        {thread.map((e) => (
          <AnswerCard
            key={e.id}
            question={e.question}
            result={e.result}
            error={e.error}
            meta={{ askedClock: e.askedClock, scopeLabel: e.scopeLabel }}
            onRetry={() => ask(e.question, e.docIds, e.id)}
            chunksByDocId={chunksByDocId}
          />
        ))}
      </div>
    </div>
  );
}
