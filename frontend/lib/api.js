/**
 * Alpha Detective fetch layer — the ONLY place `fetch` is called (CONTRACTS §4.1).
 * Every path is relative (/api/...) and served through the next.config.mjs
 * rewrite, whose destination comes from BACKEND_ORIGIN (server-side, §5.1) —
 * no host is ever hardcoded here.
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

/* ── Access code (CONTRACTS §1.10 / §4.1) ─────────────────────────────────────
   The code is a QUOTA GATE, not a secret and not a password: it exists so a
   public demo URL can't burn the free Gemini quota. It is held in module
   memory only — deliberately NOT localStorage, so it never outlives the tab
   and never lands in persistent browser storage. A build-time
   NEXT_PUBLIC_ACCESS_CODE (Vercel) seeds it; a code typed into the prompt
   overrides that for the session. GOOGLE_API_KEY never comes near this file. */

const BUILD_ACCESS_CODE = process.env.NEXT_PUBLIC_ACCESS_CODE || null;

let sessionAccessCode = null; // in-memory only, cleared on reload
const unauthorizedListeners = new Set();

/** The code that will be sent, or null. Session value wins over build-time. */
export function getAccessCode() {
  return sessionAccessCode ?? BUILD_ACCESS_CODE;
}

/** Store a code for this tab. Empty/blank clears it. No persistence. */
export function setAccessCode(code) {
  const next = typeof code === "string" ? code.trim() : "";
  sessionAccessCode = next.length > 0 ? next : null;
  return sessionAccessCode;
}

export function clearAccessCode() {
  sessionAccessCode = null;
}

/**
 * Subscribe to 401s from any call. Returns an unsubscribe fn. AppShell uses
 * this to raise the access-code prompt no matter which page made the call.
 */
export function onUnauthorized(listener) {
  unauthorizedListeners.add(listener);
  return () => unauthorizedListeners.delete(listener);
}

function emitUnauthorized(error) {
  for (const listener of unauthorizedListeners) {
    try {
      listener(error);
    } catch {
      /* a broken listener must never break the fetch layer */
    }
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
  const code = getAccessCode();
  // Headers are merged, never replaced: multipart uploads must keep letting
  // the browser set Content-Type (boundary), so we only ever add our own key.
  const init = code
    ? { ...opts, headers: { ...(opts.headers || {}), "X-Access-Code": code } }
    : opts;

  let res;
  try {
    res = await fetch(path, init);
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
      const err = new ApiError({
        code: env.code,
        message: typeof env.message === "string" ? env.message : "Request failed",
        status: res.status,
        retryAfterS: Number.isFinite(env.retry_after_s) ? env.retry_after_s : null,
      });
      // §1.10: any 401 means the ACCESS_CODE gate is on and our code is
      // missing or wrong. Raise it globally so one prompt serves every page.
      if (err.code === "unauthorized") emitUnauthorized(err);
      throw err;
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

/**
 * GET /api/health → {status, provider, llm_model, embed_model, rerank,
 * documents, chunks, chroma_ok, llm_budget?}. The ONLY route exempt from the
 * access-code gate (§1.2 law), so the poll works before a code is entered.
 */
export function getHealth() {
  return apiFetch("/api/health");
}

/** GET /api/documents → {documents:[…], totals:{documents, chunks, pages, tables}} */
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
 * GET /api/documents/{id}/chunks → §1.8 {chunks:[{chunk_ix, page, chars,
 * has_table, preview}]}. Read-only chunk inventory: no LLM, no embeddings,
 * no re-parsing, and exempt from the per-IP throttle (§1.10).
 */
export function getDocumentChunks(id) {
  return apiFetch(`/api/documents/${encodeURIComponent(id)}/chunks`);
}

/**
 * POST /api/query → §1.6 response. Accepts camelCase (CONTRACTS §4.1) and
 * wire-shaped snake_case keys; empty docIds is omitted (= all documents).
 * `explain` (§1.9) is omitted entirely when falsy — never sent as false.
 */
export function postQuery({ question, docIds, topK, explain, doc_ids, top_k } = {}) {
  const ids = docIds ?? doc_ids;
  const k = topK ?? top_k;

  const payload = { question };
  if (Array.isArray(ids) && ids.length > 0) payload.doc_ids = ids;
  if (k != null) payload.top_k = k;
  if (explain) payload.explain = true;

  return apiFetch("/api/query", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

/** Alias: query({question, doc_ids, top_k}). Same function as postQuery. */
export const query = postQuery;
