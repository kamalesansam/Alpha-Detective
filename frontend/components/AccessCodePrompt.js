"use client";

import { useState } from "react";
import { KeyRound, Loader2 } from "lucide-react";
import { listDocuments, setAccessCode } from "@/lib/api";

/**
 * The ACCESS_CODE gate (CONTRACTS §1.10 / §4.1). Raised by AppShell the first
 * time any call returns 401 `unauthorized`.
 *
 * The code is a QUOTA GATE — it keeps a public demo URL from burning the free
 * Gemini quota. It grants nothing and hides nothing, so the copy never calls
 * it a password and the input is not masked. It is held in module memory in
 * lib/api.js for this tab only: never localStorage, never a cookie, never
 * logged. `GOOGLE_API_KEY` is server-side and unrelated.
 *
 * Submitting stores the code, then verifies it with one gated GET
 * (`/api/documents`, throttle-exempt). Success dismisses the prompt and the
 * header is attached to every subsequent call automatically.
 */
export default function AccessCodePrompt({ onUnlocked, onRejected }) {
  const [value, setValue] = useState("");
  const [checking, setChecking] = useState(false);
  const [problem, setProblem] = useState(null);

  async function submit(e) {
    e.preventDefault();
    const code = value.trim();
    if (!code || checking) return;
    setChecking(true);
    setProblem(null);
    onRejected?.(false);
    setAccessCode(code);
    try {
      await listDocuments();
      setValue("");
      onUnlocked();
    } catch (err) {
      if (err.code === "unauthorized") {
        setAccessCode(""); // don't keep sending a code we know is wrong
        setProblem(err.message || "Invalid access code");
        onRejected?.(true); // now something has actually failed — banner earns its red
      } else if (err.code === "offline") {
        setProblem("Backend offline — could not check the code");
      } else {
        setProblem(err.message || "Could not check the code");
      }
    } finally {
      setChecking(false);
    }
  }

  return (
    <section
      data-testid="access-code-prompt"
      className="flex flex-col gap-3 rounded-card border border-border bg-surface px-5 py-4 shadow-card"
    >
      <div className="flex items-center gap-2">
        <KeyRound size={14} strokeWidth={1.5} className="shrink-0 text-text-3" aria-hidden="true" />
        <span className="text-[11px] font-semibold uppercase tracking-[0.06em] text-text-3">
          Access code
        </span>
      </div>

      <p className="text-[13px] leading-normal text-text-2">
        This demo limits daily AI usage. Enter the code to continue — it is kept in memory for this
        tab only and is never stored.
      </p>

      <form onSubmit={submit} className="flex gap-3">
        <input
          data-testid="access-code-input"
          type="text"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder="Enter access code"
          aria-label="Access code"
          autoComplete="off"
          autoCorrect="off"
          spellCheck={false}
          disabled={checking}
          className="h-10 min-w-0 flex-1 rounded-control border border-border-strong bg-surface px-3 font-mono text-sm text-text placeholder:font-sans placeholder:text-text-3 disabled:opacity-60"
        />
        <button
          data-testid="access-code-submit"
          type="submit"
          disabled={checking || !value.trim()}
          className="inline-flex h-10 shrink-0 items-center gap-2 rounded-control bg-accent px-4 text-sm font-medium text-surface hover:bg-accent-hover disabled:cursor-not-allowed disabled:opacity-50"
        >
          {checking ? (
            <Loader2 size={16} strokeWidth={1.5} className="animate-spin" aria-hidden="true" />
          ) : null}
          <span>Continue</span>
        </button>
      </form>

      {problem ? (
        <div data-testid="access-code-problem" role="alert" className="text-[13px] text-negative">
          {problem}
        </div>
      ) : null}
    </section>
  );
}
