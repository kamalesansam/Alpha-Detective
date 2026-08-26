"use client";

import { useRef, useState } from "react";
import { Check, Info, Loader2, Upload, X } from "lucide-react";
import ErrorBanner from "./ErrorBanner";
import { uploadDocuments } from "@/lib/api";
import { describeIngest } from "@/lib/format";

// The frozen §1.3 list — ten extensions, `ingest.ALLOWED_EXTS`. Keep this in
// the same order as the contract so it can be diffed against it at a glance.
const ACCEPT = ".pdf,.docx,.txt,.md,.csv,.xlsx,.pptx,.html,.htm,.json";
// Human-facing list folds the .htm alias into HTML — listing both reads as
// padding the count (design r3 m-5). `accept` above still carries both.
const FORMATS = "PDF, DOCX, TXT, MD, CSV, XLSX, PPTX, HTML, JSON";

/**
 * Drag/click multi-file upload. Per-file status list from the POST response:
 * spinner while the request is in flight, then check + chunk count / cross +
 * error per file; duplicates surface as a neutral "already indexed" notice.
 * Caps are enforced server-side (25 MB/file, 20 files) — request-level
 * failures land in an ErrorBanner with retry.
 */
export default function UploadDropzone({ onUploaded, disabled }) {
  const inputRef = useRef(null);
  const lastFilesRef = useRef(null);
  const [dragOver, setDragOver] = useState(false);
  const [busy, setBusy] = useState(false);
  const [items, setItems] = useState([]); // {name, state, chunks, error}
  const [requestError, setRequestError] = useState(null);

  const blocked = disabled || busy;

  async function handleFiles(fileList) {
    const files = Array.from(fileList || []);
    if (files.length === 0 || blocked) return;
    lastFilesRef.current = files;
    setRequestError(null);
    setItems(files.map((f) => ({ name: f.name, state: "uploading" })));
    setBusy(true);
    try {
      const res = await uploadDocuments(files);
      const entries = res.documents || [];
      // Response entries come back in upload order (CONTRACTS §1.3).
      setItems(
        files.map((f, i) => {
          const e = entries[i];
          if (!e) return { name: f.name, state: "failed", error: "no result returned" };
          return {
            name: e.name || f.name,
            state: e.status,
            pages: e.pages,
            tables: e.tables,
            chunks: e.chunks,
            error: e.error,
          };
        })
      );
      onUploaded(entries);
    } catch (err) {
      setRequestError(err);
      setItems((prev) =>
        prev.map((it) => ({
          ...it,
          state: "failed",
          error: err.code === "offline" ? "backend offline" : "not uploaded",
        }))
      );
    } finally {
      setBusy(false);
      if (inputRef.current) inputRef.current.value = "";
    }
  }

  function openPicker() {
    if (!blocked && inputRef.current) inputRef.current.click();
  }

  function onDrop(e) {
    e.preventDefault();
    setDragOver(false);
    if (!blocked) handleFiles(e.dataTransfer?.files);
  }

  return (
    <section className="flex flex-col gap-3">
      <div
        data-testid="dropzone"
        role="button"
        tabIndex={0}
        aria-label={`Upload documents — ${FORMATS}; max 25 MB each, up to 20 files`}
        aria-disabled={blocked || undefined}
        onClick={openPicker}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            openPicker();
          }
        }}
        onDragOver={(e) => {
          e.preventDefault();
          if (!blocked) setDragOver(true);
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={onDrop}
        className={`flex flex-col items-center gap-2 rounded-card border bg-surface px-6 py-8 shadow-card ${
          dragOver ? "border-accent bg-accent-soft" : "border-border-strong"
        } ${blocked ? "cursor-not-allowed opacity-60" : "cursor-pointer"}`}
      >
        <Upload size={20} strokeWidth={1.5} className="text-text-3" aria-hidden="true" />
        <div className="text-sm font-medium text-text">Drop files or click to upload</div>
        <div className="text-xs text-text-3">{FORMATS} · max 25 MB · up to 20 files</div>
      </div>
      <input
        data-testid="file-input"
        ref={inputRef}
        type="file"
        multiple
        accept={ACCEPT}
        onChange={(e) => handleFiles(e.target.files)}
        className="hidden"
        aria-hidden="true"
        tabIndex={-1}
      />

      {requestError ? (
        <ErrorBanner
          message={
            requestError.code === "offline"
              ? "Backend offline — run `make dev` to start the API"
              : requestError.message || "Upload failed"
          }
          retryAfterS={requestError.code === "rate_limited" ? (requestError.retryAfterS ?? 30) : null}
          onRetry={() => handleFiles(lastFilesRef.current)}
        />
      ) : null}

      {items.length > 0 ? (
        <ul data-testid="upload-progress" className="flex flex-col gap-2">
          {items.map((it, i) =>
            it.state === "duplicate" ? (
              <li
                key={`${it.name}-${i}`}
                className="flex items-center gap-3 rounded-card border border-border bg-surface px-4 py-3 shadow-card"
              >
                <Info size={16} strokeWidth={1.5} className="shrink-0 text-text-3" aria-hidden="true" />
                <span className="min-w-0 flex-1 truncate text-[13px] font-medium text-text-2">
                  {it.name} is already indexed — upload skipped.
                </span>
                <span className="shrink-0 font-mono text-[11px] text-text-3">
                  {describeIngest(it)}
                </span>
              </li>
            ) : (
              <li
                key={`${it.name}-${i}`}
                className="flex items-center gap-3 rounded-card border border-border bg-surface px-4 py-3 shadow-card"
              >
                {it.state === "uploading" ? (
                  <Loader2 size={16} strokeWidth={1.5} className="shrink-0 animate-spin text-accent" aria-hidden="true" />
                ) : it.state === "indexed" ? (
                  <Check size={16} strokeWidth={1.5} className="shrink-0 text-positive" aria-hidden="true" />
                ) : (
                  <X size={16} strokeWidth={1.5} className="shrink-0 text-negative" aria-hidden="true" />
                )}
                <span className="min-w-0 flex-1 truncate text-[13px] font-medium text-text">{it.name}</span>
                <span
                  className={`min-w-0 max-w-[55%] break-words text-right font-mono text-[11px] ${
                    it.state === "failed" ? "text-negative" : "shrink-0 text-text-3"
                  }`}
                >
                  {it.state === "uploading"
                    ? "indexing…"
                    : it.state === "indexed"
                      ? describeIngest(it)
                      : it.error || "failed"}
                </span>
              </li>
            )
          )}
        </ul>
      ) : null}
    </section>
  );
}
