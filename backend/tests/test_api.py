"""Endpoint contract tests (CONTRACTS.md SS1) -- keyless, PROVIDER=none, RERANK=off.

Every endpoint's success shape, request validation via the `bad_request`
envelope (FastAPI's raw 422 must never surface), the not_found rules, and the
leak-free error envelope. Read-only checks share a module-scoped indexed app;
mutating checks build their own.
"""

import uuid
from datetime import datetime

import pytest

HEALTH_KEYS = {"status", "provider", "llm_model", "embed_model", "rerank", "documents", "chunks", "chroma_ok"}
LIST_ENTRY_KEYS = {"id", "name", "ext", "size_bytes", "pages", "chunks", "uploaded_at", "status"}
QUERY_KEYS = {"answer", "mode", "no_answer", "model", "citations", "timings"}
CITATION_KEYS = {"n", "doc_id", "doc_name", "page", "snippet", "score"}


# --------------------------------------------------------------------------
# GET /api/health
# --------------------------------------------------------------------------
def test_health_shape(indexed_stack):
    resp = indexed_stack.client.get("/api/health")
    assert resp.status_code == 200
    h = resp.json()
    assert HEALTH_KEYS.issubset(h.keys()), f"health missing keys: {HEALTH_KEYS - set(h)}"
    assert h["status"] == "ok"
    assert h["provider"] == "none"
    assert h["llm_model"] is None and h["embed_model"] is None, "models must be null in none mode"
    assert h["rerank"] in ("on", "off")
    assert h["documents"] == 3
    assert h["chunks"] == sum(e["chunks"] for e in indexed_stack.docs.values())
    assert h["chroma_ok"] is True


# --------------------------------------------------------------------------
# POST /api/documents (success shape) + GET /api/documents (totals math)
# --------------------------------------------------------------------------
def test_upload_success_shape(stack, qa):
    paths = list(stack.samples.values())
    resp = qa.upload_paths(stack.client, paths)
    assert resp.status_code == 200, resp.text[:400]
    body = resp.json()
    assert set(body.keys()) == {"documents"}
    entries = body["documents"]
    assert [e["name"] for e in entries] == [p.name for p in paths], "entries must be in upload order"
    ids = set()
    for entry, path in zip(entries, paths):
        assert entry["status"] == "indexed", f"{entry}"
        assert qa.UUID4_RE.match(entry["id"]), f"id not UUIDv4: {entry['id']!r}"
        ids.add(entry["id"])
        assert entry["size_bytes"] == len(path.read_bytes()), f"size_bytes wrong for {path.name}"
        assert entry["chunks"] >= 1
        assert not entry.get("error"), "error field must be absent on indexed entries"
        if path.suffix == ".pdf":
            assert isinstance(entry["pages"], int) and entry["pages"] >= 1
        else:
            assert entry["pages"] is None
    assert len(ids) == 3, "ids must be distinct"


def test_list_totals_math(indexed_stack, qa):
    resp = indexed_stack.client.get("/api/documents")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"documents", "totals"}
    docs = body["documents"]
    assert len(docs) == 3
    for entry in docs:
        assert LIST_ENTRY_KEYS.issubset(entry.keys()), f"list entry missing keys: {entry}"
        assert "sha256" not in entry, "sha256 is internal and must not be exposed"
        assert entry["status"] == "indexed"
        assert entry["ext"] == "." + entry["name"].rsplit(".", 1)[1], f"ext mismatch: {entry}"
        assert qa.ISO_Z_RE.match(entry["uploaded_at"]), f"uploaded_at not ISO-8601 Z: {entry['uploaded_at']!r}"
    stamps = [datetime.fromisoformat(e["uploaded_at"].replace("Z", "+00:00")) for e in docs]
    assert stamps == sorted(stamps, reverse=True), "documents must be sorted uploaded_at desc"
    totals = body["totals"]
    assert totals["documents"] == len(docs)
    assert totals["chunks"] == sum(e["chunks"] for e in docs)
    assert totals["pages"] == sum(e["pages"] for e in docs if e["pages"] is not None)
    # totals must agree with health
    h = indexed_stack.client.get("/api/health").json()
    assert (h["documents"], h["chunks"]) == (totals["documents"], totals["chunks"])


# --------------------------------------------------------------------------
# DELETE /api/documents/{id}
# --------------------------------------------------------------------------
def test_delete_lifecycle(stack, qa):
    up = qa.upload_paths(stack.client, [stack.samples["northwind"]])
    doc_id = up.json()["documents"][0]["id"]

    qa.assert_error_envelope(
        stack.client.delete(f"/api/documents/{uuid.uuid4()}"), status=404, code="not_found"
    )
    qa.assert_error_envelope(
        stack.client.delete("/api/documents/not-a-uuid"), status=404, code="not_found"
    )

    resp = stack.client.delete(f"/api/documents/{doc_id}")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    listing = stack.client.get("/api/documents").json()
    assert listing == {"documents": [], "totals": {"documents": 0, "chunks": 0, "pages": 0, "tables": 0}}

    # not idempotent: a second DELETE is 404
    qa.assert_error_envelope(
        stack.client.delete(f"/api/documents/{doc_id}"), status=404, code="not_found"
    )

    # deleted content is gone from retrieval too
    q = qa.query(stack.client, "What was Northwind Retail's revenue in Q2 2026?")
    assert q.status_code == 200
    body = q.json()
    assert body["no_answer"] is True and body["citations"] == []
    assert "$1.84" in stack.samples["northwind"].read_text(errors="replace")  # figure exists in source
    assert "$1.84" not in q.text, "deleted document's content still retrievable"


# --------------------------------------------------------------------------
# POST /api/query -- request validation (bad_request envelope, never raw 422)
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({}, id="missing-question"),
        pytest.param({"question": ""}, id="empty-question"),
        pytest.param({"question": "   "}, id="whitespace-question"),
        pytest.param({"question": "x" * 2001}, id="question-over-2000-chars"),
        pytest.param({"question": 42}, id="question-wrong-type"),
        pytest.param({"question": "What was revenue?", "top_k": 0}, id="top_k-below-range"),
        pytest.param({"question": "What was revenue?", "top_k": 13}, id="top_k-above-range"),
        pytest.param(
            {"question": "What was revenue?", "doc_ids": [str(uuid.uuid4()) for _ in range(21)]},
            id="more-than-20-doc_ids",
        ),
    ],
)
def test_query_invalid_payload_bad_request(indexed_stack, qa, payload):
    resp = indexed_stack.client.post("/api/query", json=payload)
    assert resp.status_code != 422, "raw FastAPI 422 must be remapped to the bad_request envelope"
    qa.assert_error_envelope(resp, status=400, code="bad_request")


def test_query_malformed_json_bad_request(indexed_stack, qa):
    resp = indexed_stack.client.post(
        "/api/query", content=b'{"question": broken', headers={"Content-Type": "application/json"}
    )
    assert resp.status_code != 422
    qa.assert_error_envelope(resp, status=400, code="bad_request")


def test_query_unknown_doc_id_not_found_names_id(indexed_stack, qa):
    ghost = str(uuid.uuid4())
    resp = qa.query(indexed_stack.client, "What was revenue?", doc_ids=[ghost])
    err = qa.assert_error_envelope(resp, status=404, code="not_found")
    assert ghost in err["message"], "not_found must name the first offending doc_id"


def test_query_malformed_doc_id_rejected(indexed_stack, qa):
    # Contract: doc_ids entries must be UUIDv4 AND present in the manifest.
    # A malformed entry may surface as validation (400 bad_request) or as
    # unknown-id (404 not_found); either satisfies SS1.6 -- never a 500/422.
    resp = qa.query(indexed_stack.client, "What was revenue?", doc_ids=["../../etc/passwd"])
    assert resp.status_code in (400, 404), f"malformed doc_id: HTTP {resp.status_code}: {resp.text[:200]}"
    err = qa.assert_error_envelope(resp)
    assert err["code"] in ("bad_request", "not_found")


# --------------------------------------------------------------------------
# POST /api/query -- response shapes
# --------------------------------------------------------------------------
def test_query_empty_corpus_refuses(stack, qa):
    """Empty corpus => no_answer:true, never an HTTP error (SS1.6)."""
    resp = qa.query(stack.client, "What was Meridian's Q2 FY2026 revenue?")
    assert resp.status_code == 200, resp.text[:300]
    body = resp.json()
    assert body["no_answer"] is True
    assert body["answer"] == qa.REFUSAL
    assert body["citations"] == []


def test_query_extractive_response_shape(indexed_stack, qa):
    resp = qa.query(indexed_stack.client, "What was Meridian's Q2 FY2026 revenue?")
    assert resp.status_code == 200, resp.text[:300]
    body = resp.json()
    assert QUERY_KEYS.issubset(body.keys()), f"query response missing keys: {QUERY_KEYS - set(body)}"
    assert body["mode"] == "extractive", "PROVIDER=none must answer extractively"
    assert body["model"] is None
    assert isinstance(body["no_answer"], bool) and body["no_answer"] is False
    assert body["timings"]["llm_ms"] == 0, "extractive mode spends zero LLM calls"

    citations = body["citations"]
    assert citations, "extractive answer with no citations"
    assert len(citations) <= 6, "default top_k is 6"
    assert [c["n"] for c in citations] == list(range(1, len(citations) + 1)), (
        f"citation n must be contiguous from 1: {[c['n'] for c in citations]}"
    )
    for c in citations:
        assert CITATION_KEYS.issubset(c.keys()), f"citation missing keys: {c}"
        assert qa.UUID4_RE.match(c["doc_id"])
        assert isinstance(c["doc_name"], str) and c["doc_name"]
        assert c["page"] is None or isinstance(c["page"], int)
        assert isinstance(c["snippet"], str) and c["snippet"].strip()
        assert len(c["snippet"]) <= 302, f"snippet exceeds 300 chars (+ellipsis): {len(c['snippet'])}"
        assert not c["snippet"].startswith(f"[{c['doc_name']}"), (
            f"snippet must not carry the provenance prefix: {c['snippet'][:60]!r}"
        )
        assert isinstance(c["score"], float)
        assert abs(c["score"] - round(c["score"], 4)) < 1e-9, f"score not rounded to 4dp: {c['score']}"

    # extractive answer format: paragraphs "[n] <snippet>" joined by blank lines, top min(3, len)
    answer = body["answer"]
    assert answer.startswith("[1] "), f"extractive answer must start with '[1] ': {answer[:60]!r}"
    if len(citations) >= 2:
        assert "[2] " in answer
    paragraphs = [p for p in answer.split("\n\n") if p.strip()]
    assert not any(p.lstrip().startswith("[4]") for p in paragraphs), (
        "extractive answer must use at most the top 3 snippets"
    )


def test_query_top_k_bounds_respected(indexed_stack, qa):
    for top_k in (1, 2, 12):
        resp = qa.query(indexed_stack.client, "What was Meridian's Q2 FY2026 revenue?", top_k=top_k)
        assert resp.status_code == 200, resp.text[:200]
        assert len(resp.json()["citations"]) <= top_k, f"top_k={top_k} not respected"


# --------------------------------------------------------------------------
# Error envelope hygiene, systematically
# --------------------------------------------------------------------------
def test_error_envelopes_never_leak_internals(indexed_stack, qa):
    client = indexed_stack.client
    error_responses = [
        client.post("/api/query", json={}),
        client.delete(f"/api/documents/{uuid.uuid4()}"),
        client.delete("/api/documents/definitely-not-a-uuid"),
        client.post("/api/documents"),
        qa.query(client, "What was revenue?", doc_ids=[str(uuid.uuid4())]),
    ]
    for resp in error_responses:
        # assert_error_envelope enforces: envelope-only body, known code, and no
        # "Traceback"/site-packages/filesystem-path leakage anywhere in the response.
        qa.assert_error_envelope(resp)
