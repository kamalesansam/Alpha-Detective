/**
 * Alpha Detective fetch layer — the ONLY place `fetch` is called (CONTRACTS §4.1).
 * Every path is relative (/api/...) and served through the next.config.mjs
 * rewrite to FastAPI on 127.0.0.1:8000 — no host is ever hardcoded here.
 */

export class ApiError extends Error {
  constructor({ code, message, status, retryAfterS = null }) {
    super(message);
    this.name = "ApiError";
    this.code = code; // §1.1 envelope code, or "offline"
    this.status = status; // HTTP status; 0 when the request never completed
    this.retryAfterS = retryAfterS; // seconds; set for code "rate_limited"
  }

  // Alias mirroring the wire envelope's field name.
  get retry_after_s() {
    return this.retryAfterS;
  }
}

/**
 * Core wrapper. Resolves with parsed JSON on 2xx. Throws ApiError:
 *  - network/connection failure          → {code:"offline", status:0}
 *  - non-2xx carrying the §1.1 envelope  → {code, message, status, retryAfterS}
 *  - non-2xx WITHOUT the envelope        → {code:"offline", status} — the backend
 *    wraps every non-2xx in the envelope with no exceptions, so an envelope-less
 *    error (e.g. the Next rewrite failing with an HTML 500 because uvicorn is
 *    down) can only mean the backend is unreachable.
 */
export async function apiFetch(path, opts = {}) {
  let res;
  try {
    res = await fetch(path, opts);
  } catch {
    throw new ApiError({ code: "offline", message: "Backend offline", status: 0 });
  }

  let body = null;
  try {
    body = await res.json();
  } catch {
    body = null;
  }

  if (!res.ok) {
    const env = body && typeof body === "object" ? body.error : null;
    if (env && typeof env.code === "string") {
      throw new ApiError({
        code: env.code,
        message: typeof env.message === "string" ? env.message : "Request failed",
        status: res.status,
        retryAfterS: Number.isFinite(env.retry_after_s) ? env.retry_after_s : null,
      });
    }
    throw new ApiError({ code: "offline", message: "Backend offline", status: res.status });
  }

  if (body === null || typeof body !== "object") {
    throw new ApiError({
      code: "internal",
      message: "Invalid response from backend",
      status: res.status,
    });
  }
  return body;
}

/** GET /api/health → {status, provider, llm_model, embed_model, rerank, documents, chunks, chroma_ok} */
export function getHealth() {
  return apiFetch("/api/health");
}

/** GET /api/documents → {documents:[…], totals:{documents, chunks, pages}} */
export function listDocuments() {
  return apiFetch("/api/documents");
}

/**
 * POST /api/documents (multipart). `files`: FileList or File[].
 * Field name is `files`, repeated; Content-Type is left to the browser so the
 * multipart boundary is set correctly. → {documents:[{id, name, …, status}]}
 */
export function uploadDocuments(files) {
  const form = new FormData();
  for (const file of Array.from(files ?? [])) {
    form.append("files", file);
  }
  return apiFetch("/api/documents", { method: "POST", body: form });
}

/** DELETE /api/documents/{id} → {ok:true} */
export function deleteDocument(id) {
  return apiFetch(`/api/documents/${encodeURIComponent(id)}`, { method: "DELETE" });
}

/**
 * POST /api/query → §1.6 response. Accepts camelCase (CONTRACTS §4.1) and
 * wire-shaped snake_case keys; empty docIds is omitted (= all documents).
 */
export function postQuery({ question, docIds, topK, doc_ids, top_k } = {}) {
  const ids = docIds ?? doc_ids;
  const k = topK ?? top_k;

  const payload = { question };
  if (Array.isArray(ids) && ids.length > 0) payload.doc_ids = ids;
  if (k != null) payload.top_k = k;

  return apiFetch("/api/query", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

/** Alias: query({question, doc_ids, top_k}). Same function as postQuery. */
export const query = postQuery;
