"use client";

import { useEffect, useState } from "react";
import { AlertTriangle } from "lucide-react";

// Backtick spans in the message render as inline code (e.g. `make dev`).
function renderMessage(message) {
  return String(message)
    .split("`")
    .map((part, i) =>
      i % 2 === 1 ? (
        <code key={i} className="rounded-[4px] bg-negative/10 px-1 font-mono text-xs">
          {part}
        </code>
      ) : (
        <span key={i}>{part}</span>
      )
    );
}

/**
 * All fetch errors funnel here (CONTRACTS §4.2). With retryAfterS set it shows
 * a live countdown ("retry in ~24 s.") and surfaces the Retry action at zero;
 * with onRetry alone the Retry action is immediate.
 */
export default function ErrorBanner({ message, retryAfterS, onRetry }) {
  const hasCountdown = typeof retryAfterS === "number" && retryAfterS > 0;
  const [left, setLeft] = useState(hasCountdown ? Math.ceil(retryAfterS) : 0);
  const [lastRetryAfterS, setLastRetryAfterS] = useState(retryAfterS);

  // Reset the countdown when a new retry window arrives (render-time state
  // adjustment — no setState inside the effect body).
  if (retryAfterS !== lastRetryAfterS) {
    setLastRetryAfterS(retryAfterS);
    setLeft(hasCountdown ? Math.ceil(retryAfterS) : 0);
  }

  useEffect(() => {
    if (!hasCountdown) return undefined;
    const id = setInterval(() => {
      setLeft((s) => (s <= 1 ? 0 : s - 1));
    }, 1000);
    return () => clearInterval(id);
  }, [retryAfterS, hasCountdown]);

  const showRetry = onRetry && (!hasCountdown || left === 0);

  return (
    <div
      data-testid="error-banner"
      role="alert"
      className="flex items-start gap-2 rounded-control border border-negative/20 bg-negative-soft px-3 py-2.5"
    >
      <AlertTriangle size={14} strokeWidth={1.5} className="mt-0.5 shrink-0 text-negative" aria-hidden="true" />
      <div className="text-[13px] leading-normal text-negative">
        {renderMessage(message)}
        {hasCountdown ? (left > 0 ? ` — retry in ~${left} s.` : " — you can retry now.") : "."}
        {showRetry ? (
          <>
            {" "}
            <button
              type="button"
              onClick={onRetry}
              className="font-semibold underline underline-offset-2"
            >
              Retry now
            </button>
          </>
        ) : null}
      </div>
    </div>
  );
}
