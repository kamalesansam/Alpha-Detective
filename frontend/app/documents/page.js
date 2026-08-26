"use client";

import { useEffect, useState } from "react";
import { useApp } from "@/components/AppShell";
import UploadDropzone from "@/components/UploadDropzone";
import DocumentsTable from "@/components/DocumentsTable";
import EmptyState from "@/components/EmptyState";
import ErrorBanner from "@/components/ErrorBanner";
import Skeleton from "@/components/Skeleton";
import { deleteDocument, listDocuments } from "@/lib/api";

export default function DocumentsPage() {
  const { offline, refreshKey, requestRefresh } = useApp();
  const [docs, setDocs] = useState(null); // {documents, totals} | null while loading
  const [busyId, setBusyId] = useState(null);
  const [actionError, setActionError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    listDocuments()
      .then((d) => {
        if (!cancelled) setDocs(d);
      })
      .catch(() => {
        /* offline banner (AppShell) explains; retried when offline clears */
      });
    return () => {
      cancelled = true;
    };
  }, [refreshKey, offline]);

  async function handleDelete(id) {
    const doc = docs?.documents?.find((d) => d.id === id);
    if (!doc || busyId) return;
    const ok = window.confirm(
      `Delete "${doc.name}" and its ${doc.chunks} indexed ${doc.chunks === 1 ? "chunk" : "chunks"}? This cannot be undone.`
    );
    if (!ok) return;
    setBusyId(id);
    setActionError(null);
    try {
      await deleteDocument(id);
      requestRefresh(); // refetches the list and the sidebar count
    } catch (err) {
      setActionError(err);
    } finally {
      setBusyId(null);
    }
  }

  return (
    <div className="flex flex-col gap-6">
      <UploadDropzone disabled={offline} onUploaded={() => requestRefresh()} />

      {actionError ? (
        <ErrorBanner
          message={
            actionError.code === "offline"
              ? "Backend offline — run `make dev` to start the API"
              : actionError.code === "not_found"
                ? "That document is already gone — refreshing the list"
                : actionError.message || "Delete failed"
          }
        />
      ) : null}

      {!docs ? (
        <div className="rounded-card border border-border bg-surface p-5 shadow-card">
          <Skeleton lines={4} />
        </div>
      ) : docs.documents.length === 0 ? (
        <EmptyState
          testId="docs-empty"
          title="No documents yet"
          message="Upload your first filing above to build the index."
          actionLabel="Choose files"
          onAction={() => document.querySelector('[data-testid="file-input"]')?.click()}
        />
      ) : (
        <DocumentsTable documents={docs.documents} onDelete={handleDelete} busyId={busyId} />
      )}
    </div>
  );
}
