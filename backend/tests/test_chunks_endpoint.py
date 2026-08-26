"""GET /api/documents/{id}/chunks -- CONTRACTS.md v1.2 SS1.8 (+ SS3.3 store law).

The endpoint is a PURE READ of the docstore nodes ingest already committed:
zero LLM calls, zero embedding calls, zero re-parsing. It must also satisfy the
store-consistency law -- `len(chunks)` equals the manifest's `chunks` for that
document -- and it must 404 with the SS1.1 envelope on a malformed id BEFORE it
touches the store (path-traversal defense, identical semantics to DELETE SS1.5).

Keyless throughout; the provider tripwire proves the "zero calls" clause.
"""

import uuid

import pytest

from conftest import (
    SAMPLE_FILENAMES,
    no_provider_calls,
    upload_bytes,
)

ROW_KEYS = {"chunk_ix", "page", "chars", "has_table", "preview"}

# Ids that DO reach the handler as a single path segment: SS1.8 freezes the
# message as `unknown document id` for every one of them.
MALFORMED_IDS = [
    "not-a-uuid",
    "6f1c2a34-9b1d-4e2a-8c55",
    "6f1c2a34-9b1d-1e2a-8c55-2f8a01d9b7aa",  # version nibble is 1, not 4
    "6f1c2a34-9b1d-4e2a-0c55-2f8a01d9b7aa",  # variant nibble out of [89ab]
    "6F1C2A34-9B1D-4E2A-8C55-2F8A01D9B7AA-extra",
    " ",
    "%2e%2e",
    "null",
]

# Ids that change the URL SHAPE and are refused by routing before the handler
# ever runs. The frozen message does not apply; the envelope and the no-leak
# rule still do.
ROUTE_BREAKING_IDS = ["../../etc/passwd", "..%2F..%2Fetc%2Fpasswd", ""]


def chunks_of(client, doc_id):
    resp = client.get(f"/api/documents/{doc_id}/chunks")
    assert resp.status_code == 200, (
        f"SS1.8 chunks endpoint failed for {doc_id}: HTTP {resp.status_code} {resp.text[:400]}"
    )
    body = resp.json()
    assert set(body) == {"chunks"}, f"SS1.8 response is exactly {{'chunks': [...]}}, got {sorted(body)}"
    return body["chunks"]


# --------------------------------------------------------------------------
# SS1.8 response shape
# --------------------------------------------------------------------------
def test_chunk_rows_match_the_frozen_field_set(indexed_stack):
    doc = indexed_stack.by_key["meridian"]
    rows = chunks_of(indexed_stack.client, doc["id"])
    assert rows, "an indexed document always has chunks >= 1 (SS3.2)"
    for row in rows:
        assert set(row) == ROW_KEYS, f"SS1.8 row fields: expected {sorted(ROW_KEYS)}, got {sorted(row)}"
        assert isinstance(row["chunk_ix"], int) and row["chunk_ix"] >= 0
        assert row["page"] is None or (isinstance(row["page"], int) and row["page"] >= 1)
        assert isinstance(row["chars"], int) and row["chars"] > 0
        assert isinstance(row["has_table"], bool), (
            f"SS1.8: has_table is a bool, never null -- got {row['has_table']!r}"
        )
        assert isinstance(row["preview"], str) and row["preview"].strip()


def test_chunk_ix_is_ascending_and_contiguous_from_zero(indexed_stack):
    for name, doc in indexed_stack.docs.items():
        rows = chunks_of(indexed_stack.client, doc["id"])
        got = [r["chunk_ix"] for r in rows]
        assert got == list(range(len(rows))), (
            f"SS1.8: chunk_ix ascends contiguously from 0 (stores.nodes_for order) "
            f"for {name}: {got}"
        )


def test_preview_is_capped_at_200_chars(indexed_stack):
    for doc in indexed_stack.docs.values():
        for row in chunks_of(indexed_stack.client, doc["id"]):
            assert len(row["preview"]) <= 200, (
                f"SS1.8: preview is <= 200 chars, got {len(row['preview'])}: "
                f"{row['preview'][:60]!r}..."
            )


def test_preview_is_a_clean_word_bounded_head(indexed_stack):
    doc = indexed_stack.by_key["meridian"]
    truncated = 0
    for row in chunks_of(indexed_stack.client, doc["id"]):
        preview = row["preview"]
        assert not preview.lstrip().startswith("["), (
            f"SS1.8: the `[doc - p.N]` provenance prefix must be removed: {preview[:60]!r}"
        )
        assert "  " not in preview and "\n" not in preview and "\t" not in preview, (
            f"SS1.8: whitespace is collapsed to single spaces: {preview[:80]!r}"
        )
        assert preview == preview.strip(), f"preview must not have edge whitespace: {preview!r}"
        if preview.endswith("…"):
            truncated += 1
            assert row["chars"] > len(preview), (
                "a trailing ellipsis means the chunk was longer than the preview"
            )
            assert not preview[:-1].rstrip().endswith(" "), preview[-30:]
        else:
            assert row["chars"] <= 200, (
                f"SS1.8: a chunk longer than the cap must be cut with a trailing '…' "
                f"(chars={row['chars']}, preview len={len(preview)})"
            )
    assert truncated, "expected at least one truncated preview in a real PDF corpus"


def test_chars_measures_the_provenance_stripped_text(indexed_stack, qa):
    """SS1.8: chars is the length of the PROVENANCE-STRIPPED chunk text -- the same
    text `preview` derives from -- not of the stored node text."""
    ingest = qa.backend_module("ingest")
    store = qa.backend_module("stores").get_store()
    for key in ("meridian", "helios"):
        doc = indexed_stack.by_key[key]
        rows = chunks_of(indexed_stack.client, doc["id"])
        nodes = store.nodes_for([doc["id"]])
        assert len(nodes) == len(rows), (len(nodes), len(rows))
        nodes = sorted(nodes, key=lambda n: n.metadata["chunk_ix"])
        for row, node in zip(rows, nodes):
            raw = node.get_content()
            prefix = ingest.provenance_prefix(node.metadata["doc_name"], node.metadata["page"])
            stripped = raw[len(prefix):] if raw.startswith(prefix) else raw
            assert row["chars"] == len(stripped), (
                f"SS1.8: chars must be len(provenance-stripped text). chunk_ix="
                f"{row['chunk_ix']} chars={row['chars']} expected={len(stripped)} "
                f"(raw node len={len(raw)}, prefix len={len(prefix)})"
            )
            assert row["chars"] >= len(row["preview"].rstrip("…").strip())


# --------------------------------------------------------------------------
# SS1.8 has_table correctness (block-level inheritance, resolution 11)
# --------------------------------------------------------------------------
def test_has_table_true_somewhere_in_a_table_bearing_pdf(indexed_stack):
    doc = indexed_stack.by_key["meridian"]
    rows = chunks_of(indexed_stack.client, doc["id"])
    flagged = [r for r in rows if r["has_table"]]
    assert flagged, (
        "SS1.8/SS2: the Meridian PDF carries a pdfplumber-parsed metrics table "
        f"(SS1.7 shows tables:1), so at least one chunk must set has_table -- got {rows}"
    )
    assert doc.get("tables", 0) >= 1, (
        f"SS1.3: the upload response must report tables >= 1 for this PDF, got {doc.get('tables')!r}"
    )


def test_has_table_false_everywhere_in_a_plain_text_doc(indexed_stack):
    doc = indexed_stack.by_key["northwind"]  # .txt -- verbatim single block, tables = 0
    rows = chunks_of(indexed_stack.client, doc["id"])
    assert all(r["has_table"] is False for r in rows), (
        f"SS2: TXT parses to a verbatim block with tables = 0, so has_table is False "
        f"for every chunk -- got {[r['has_table'] for r in rows]}"
    )
    assert doc.get("tables", 0) == 0, f"SS2: TXT tables = 0, got {doc.get('tables')!r}"


def test_page_is_null_for_docx_and_int_for_pdf(indexed_stack):
    pdf_rows = chunks_of(indexed_stack.client, indexed_stack.by_key["meridian"]["id"])
    assert all(isinstance(r["page"], int) and r["page"] >= 1 for r in pdf_rows), (
        f"SS1.8: PDF chunks carry an int page: {[r['page'] for r in pdf_rows]}"
    )
    docx_rows = chunks_of(indexed_stack.client, indexed_stack.by_key["helios"]["id"])
    assert all(r["page"] is None for r in docx_rows), (
        f"SS1.8: docx chunks carry page null: {[r['page'] for r in docx_rows]}"
    )


def test_missing_has_table_metadata_reads_as_false_not_corruption(indexed_stack, qa):
    """SS1.8 + SS3.4(5): pre-v1.2 nodes carry no has_table -- absence is not corruption."""
    stores = qa.backend_module("stores")
    store = qa.require_attr(stores, "get_store", "SS2 stores.py")()
    doc_id = indexed_stack.by_key["helios"]["id"]
    nodes = store.nodes_for([doc_id])
    assert nodes, "nodes_for returned nothing for an indexed document"
    saved = {n.node_id: n.metadata.pop("has_table") for n in nodes if "has_table" in n.metadata}
    try:
        if not saved or any("has_table" in n.metadata for n in store.nodes_for([doc_id])):
            pytest.skip("docstore hands out detached copies -- cannot simulate a pre-v1.2 node")
        rows = chunks_of(indexed_stack.client, doc_id)
        assert all(r["has_table"] is False for r in rows), (
            "SS1.8: nodes without has_table metadata report False, never null and never a 500"
        )
    finally:
        for node in store.nodes_for([doc_id]):
            if node.node_id in saved:
                node.metadata["has_table"] = saved[node.node_id]


# --------------------------------------------------------------------------
# SS1.8 + SS3.3 store-consistency law
# --------------------------------------------------------------------------
def test_chunk_count_equals_the_manifest_count(indexed_stack):
    listed = indexed_stack.client.get("/api/documents").json()
    for entry in listed["documents"]:
        rows = chunks_of(indexed_stack.client, entry["id"])
        assert len(rows) == entry["chunks"], (
            f"SS1.8 (law): len(chunks) must equal the manifest's chunks for "
            f"{entry['name']} -- endpoint={len(rows)}, manifest={entry['chunks']}"
        )


def test_chunk_totals_agree_with_the_list_endpoint(indexed_stack):
    listed = indexed_stack.client.get("/api/documents").json()
    total = sum(len(chunks_of(indexed_stack.client, d["id"])) for d in listed["documents"])
    assert total == listed["totals"]["chunks"], (
        f"SS1.4/SS3.3: summed chunk inventories ({total}) must equal totals.chunks "
        f"({listed['totals']['chunks']})"
    )


def test_chunks_never_reparse_the_uploaded_file(stack, qa, samples):
    """SS1.8: 'zero re-parsing' -- deleting the raw upload must not break the read."""
    docs = qa.index_samples(stack.client, samples)
    doc = docs[SAMPLE_FILENAMES["meridian"]]
    before = chunks_of(stack.client, doc["id"])
    upload_dir = stack.storage / "uploads" / doc["id"]
    assert upload_dir.is_dir(), f"SS3.1: raw bytes live at uploads/{{doc_id}}/, missing: {upload_dir}"
    for f in upload_dir.iterdir():
        f.unlink()
    after = chunks_of(stack.client, doc["id"])
    assert after == before, (
        "SS1.8: the chunk inventory is served from the docstore, so removing the raw "
        "upload must change nothing. A difference means the endpoint re-parses."
    )


def test_chunks_makes_zero_provider_calls(indexed_stack):
    doc_id = indexed_stack.by_key["meridian"]["id"]
    with no_provider_calls() as calls:
        resp = indexed_stack.client.get(f"/api/documents/{doc_id}/chunks")
    assert resp.status_code == 200, resp.text[:300]
    assert calls == [], f"SS1.8: zero LLM and zero embedding calls, got {calls}"


def test_chunks_reflect_a_new_upload_and_disappear_after_delete(stack, qa):
    body = b"alpha,beta\n1,2\n3,4\n"
    entry = upload_bytes(stack.client, [("tiny.csv", body)]).json()["documents"][0]
    assert entry["status"] == "indexed", entry
    rows = chunks_of(stack.client, entry["id"])
    assert len(rows) == entry["chunks"]
    assert stack.client.delete(f"/api/documents/{entry['id']}").status_code == 200
    gone = stack.client.get(f"/api/documents/{entry['id']}/chunks")
    qa.assert_error_envelope(gone, status=404, code="not_found")


# --------------------------------------------------------------------------
# SS1.8 404 envelope -- malformed and unknown are indistinguishable
# --------------------------------------------------------------------------
@pytest.mark.parametrize("bad", MALFORMED_IDS, ids=lambda s: (s or "empty")[:24])
def test_malformed_id_is_404_not_found(indexed_stack, qa, bad):
    resp = indexed_stack.client.get(f"/api/documents/{bad}/chunks")
    err = qa.assert_error_envelope(resp, status=404, code="not_found")
    assert err["message"] == "unknown document id", (
        f"SS1.8 freezes the message as 'unknown document id', got {err['message']!r}"
    )


def test_unknown_but_wellformed_uuid_is_404(indexed_stack, qa):
    resp = indexed_stack.client.get(f"/api/documents/{uuid.uuid4()}/chunks")
    err = qa.assert_error_envelope(resp, status=404, code="not_found")
    assert err["message"] == "unknown document id", err


@pytest.mark.parametrize("bad", ROUTE_BREAKING_IDS, ids=lambda s: (s or "empty")[:24])
def test_route_breaking_ids_are_refused_without_leaking(indexed_stack, qa, bad):
    """Path traversal must never 200 and never echo a filesystem path (SS1.1/SS1.5)."""
    resp = indexed_stack.client.get(f"/api/documents/{bad}/chunks")
    assert resp.status_code in (400, 404, 405), f"traversal must not succeed: {resp.status_code}"
    qa.assert_error_envelope(resp)
    for leak in ("etc/passwd", "storage", "uploads"):
        assert leak not in resp.text, f"error body leaks {leak!r}: {resp.text[:200]}"


def test_404_body_leaks_no_paths_or_ids_beyond_the_envelope(indexed_stack, qa):
    resp = indexed_stack.client.get("/api/documents/..%2F..%2Fstorage/chunks")
    assert resp.status_code in (404, 400), f"path traversal must not 200: {resp.status_code}"
    qa.assert_error_envelope(resp)
    assert "storage" not in resp.text, f"SS1.1: messages carry no filesystem paths: {resp.text[:200]}"


def test_wrong_method_on_chunks_path_is_an_envelope_error(indexed_stack, qa):
    doc_id = indexed_stack.by_key["meridian"]["id"]
    resp = indexed_stack.client.post(f"/api/documents/{doc_id}/chunks", json={})
    assert resp.status_code in (404, 405), f"expected 404/405, got {resp.status_code}"
    qa.assert_error_envelope(resp, code="not_found")


# --------------------------------------------------------------------------
# PROVIDER=auto -- the shipped default
# --------------------------------------------------------------------------
def test_chunks_under_auto_default(auto_indexed_stack):
    doc = auto_indexed_stack.by_key["meridian"]
    rows = chunks_of(auto_indexed_stack.client, doc["id"])
    assert len(rows) == doc["chunks"]
    assert all(set(r) == ROW_KEYS for r in rows)
