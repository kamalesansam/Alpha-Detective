"""Thin routing layer — each handler: validate -> one module call -> shape
response. Zero business logic; exception -> envelope mapping lives in main.py.
"""

from __future__ import annotations

import asyncio
import time
from typing import Optional

from fastapi import APIRouter, File, UploadFile
from pydantic import BaseModel

from . import ingest, retrieval, stores, synthesis
from . import rerank as rerank_mod
from .providers import get_bundle  # exception types + bundle only

router = APIRouter(prefix="/api")


# --- typed API errors (main.py maps them to the §1.1 envelope) ----------------

class ApiError(Exception):
    status = 500
    code = "internal"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class BadRequestError(ApiError):
    status = 400
    code = "bad_request"


class BadFileError(ApiError):
    status = 400
    code = "bad_file"


class NotFoundError(ApiError):
    status = 404
    code = "not_found"


# --- request models -----------------------------------------------------------

class QueryRequest(BaseModel):
    question: str
    doc_ids: Optional[list[str]] = None
    top_k: Optional[int] = None


# --- handlers -----------------------------------------------------------------

@router.get("/health")
async def health() -> dict:
    bundle = get_bundle()
    store = stores.get_store()
    documents, chunks, _pages = store.counts()
    return {
        "status": "ok",
        "provider": bundle.provider,
        "llm_model": bundle.llm_model_name,
        "embed_model": bundle.embed_model_name,
        "rerank": rerank_mod.effective_rerank(),
        "documents": documents,
        "chunks": chunks,
        "chroma_ok": store.chroma_ok(),
    }


@router.post("/documents")
async def upload_documents(files: Optional[list[UploadFile]] = File(None)) -> dict:
    # NOTE: the request-level Content-Length ceiling (MAX_REQUEST_BYTES) is
    # enforced by BodySizeLimitMiddleware in main.py — it must run BEFORE
    # FastAPI's multipart parsing, which happens before this handler.
    if not files:
        raise BadFileError("no files uploaded (multipart field 'files' is required)")
    if len(files) > ingest.MAX_FILES_PER_REQUEST:
        raise BadFileError(
            f"too many files: {len(files)} (max {ingest.MAX_FILES_PER_REQUEST} per request)"
        )
    uploads: list[tuple[str, bytes]] = []
    for f in files:
        # Never pull more than the per-file cap (+1 sentinel byte) into RAM;
        # an over-cap read still fails per-file downstream with HTTP 200.
        uploads.append((f.filename or "file", await f.read(ingest.MAX_FILE_BYTES + 1)))
    entries = await ingest.ingest_files(uploads)
    return {"documents": entries}


@router.get("/documents")
async def list_documents() -> dict:
    store = stores.get_store()
    docs = store.get_manifest()
    docs.sort(key=lambda d: d.get("uploaded_at") or "", reverse=True)
    documents = [
        {
            "id": d["id"],
            "name": d["name"],
            "ext": d["ext"],
            "size_bytes": d["size_bytes"],
            "pages": d["pages"],
            "chunks": d["chunks"],
            "uploaded_at": d["uploaded_at"],
            "status": d["status"],
        }
        for d in docs
    ]
    n_docs, n_chunks, n_pages = store.counts()
    return {
        "documents": documents,
        "totals": {"documents": n_docs, "chunks": n_chunks, "pages": n_pages},
    }


@router.delete("/documents/{doc_id}")
async def delete_document(doc_id: str) -> dict:
    # uuid validation happens inside ingest.delete_document BEFORE any
    # store/filesystem access (path-traversal defense).
    if not await ingest.delete_document(doc_id):
        raise NotFoundError("unknown document id")
    return {"ok": True}


@router.post("/query")
async def query(body: QueryRequest) -> dict:
    t_total = time.perf_counter()

    question = (body.question or "").strip()
    if not question or len(question) > 2000:
        raise BadRequestError("question must be between 1 and 2000 characters")

    top_k = 6 if body.top_k is None else body.top_k
    if not isinstance(top_k, int) or not (1 <= top_k <= 12):
        raise BadRequestError("top_k must be between 1 and 12")

    doc_ids: Optional[list[str]] = None
    if body.doc_ids:
        if len(body.doc_ids) > 20:
            raise BadRequestError("too many doc_ids (max 20)")
        store = stores.get_store()
        for d in body.doc_ids:
            if not isinstance(d, str) or not ingest.UUID4_RE.match(d) or store.find_by_id(d) is None:
                raise NotFoundError(f"unknown document id: {d}")
        doc_ids = list(body.doc_ids)

    result = await asyncio.to_thread(retrieval.run_retrieval, question, doc_ids, top_k)

    bundle = get_bundle()
    mode = "generative" if bundle.provider == "gemini" else "extractive"

    if result.no_answer:
        answer, no_answer, llm_ms, model = synthesis.REFUSAL_SENTENCE, True, 0, None
        citations: list[dict] = []
    elif bundle.provider == "gemini":
        syn = await asyncio.to_thread(synthesis.synthesize, question, result.nodes)
        answer, no_answer, llm_ms = syn["answer"], syn["no_answer"], syn["llm_ms"]
        model = bundle.llm_model_name
        citations = [] if no_answer else _citations(result.nodes, question)
    else:
        syn = synthesis.extractive_answer(result.nodes, question)
        answer, no_answer, llm_ms, model = syn["answer"], False, 0, None
        citations = _citations(result.nodes, question)

    total_ms = int(round((time.perf_counter() - t_total) * 1000))
    return {
        "answer": answer,
        "mode": mode,
        "no_answer": no_answer,
        "model": model,
        "citations": citations,
        "timings": {
            "retrieval_ms": result.retrieval_ms,
            "rerank_ms": result.rerank_ms,
            "llm_ms": llm_ms,
            "total_ms": total_ms,
        },
    }


def _citations(nodes: list, question: Optional[str] = None) -> list[dict]:
    out = []
    for i, nws in enumerate(nodes, start=1):
        md = nws.node.metadata or {}
        out.append(
            {
                "n": i,
                "doc_id": md.get("doc_id"),
                "doc_name": md.get("doc_name"),
                "page": md.get("page"),
                "snippet": synthesis.make_snippet(nws.node, question),
                "score": round(float(nws.score or 0.0), 4),
            }
        )
    return out
