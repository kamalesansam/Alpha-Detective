"""Ingestion contract tests (CONTRACTS.md SS1.3, SS2 ingest.py, SS3.1) -- keyless, PROVIDER=none.

Covers: per-format parsing (PDF pages + serialized tables, DOCX incl. its table,
TXT verbatim, CSV windowing), chunk metadata + provenance prefix, server-side
caps (>25MB per-file, >20 files, unsupported extension, magic-byte mismatch,
empty file), duplicate detection without re-index, and filename sanitization
(path traversal stays inside storage/uploads).

Envelope split per CONTRACTS.md SS1.3: request-level violations (zero files,
missing `files` field, >20 files) are HTTP 400 `bad_file`; per-file violations
(oversize, unsupported ext, sniff mismatch, no extractable text) are per-file
`status:"failed"` entries with HTTP 200 and nothing persisted.
"""

import importlib
import uuid
from types import SimpleNamespace

import pytest

MB = 1024 * 1024


# --------------------------------------------------------------------------
# Parse-level tests: contract functions ingest.parse_file / ingest.chunk_pages
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def ingest_mod(tmp_path_factory, qa):
    qa.load_backend(tmp_path_factory.mktemp("parse") / "storage")
    return importlib.import_module("backend.app.ingest")


@pytest.fixture(scope="module")
def reject_stack(tmp_path_factory, qa):
    """Shared empty app for rejection tests -- rejected uploads persist nothing,
    so the store stays empty and these tests remain order-independent."""
    with qa.app_client(tmp_path_factory.mktemp("reject") / "storage") as client:
        yield SimpleNamespace(client=client)


def _joined(pages):
    return "\n".join(text for _page, text in pages)


def test_parse_pdf_pages_and_table_text(ingest_mod, samples):
    pages = ingest_mod.parse_file(samples["meridian"], ".pdf")
    assert pages, "PDF parsed to zero (page, text) blocks"
    for page, text in pages:
        assert isinstance(page, int) and page >= 1, f"PDF page must be int>=1, got {page!r}"
        assert isinstance(text, str)
    combined = _joined(pages)
    assert "Meridian" in combined
    # The whole point of table-aware parsing: financial figures live in the
    # rendered metrics table and MUST be retrievable from parsed text.
    assert "$48.2" in combined, "revenue figure $48.2 not extractable from PDF text/tables"
    assert "118%" in combined, "NRR 118% (metrics-table value) not extractable from PDF"
    assert "$210.4" in combined, "ARR $210.4M (metrics-table value) not extractable from PDF"


def test_parse_docx_includes_table(ingest_mod, samples):
    pages = ingest_mod.parse_file(samples["helios"], ".docx")
    assert pages, "DOCX parsed to zero blocks"
    for page, text in pages:
        assert page is None, f"DOCX blocks carry page=None per contract, got {page!r}"
        assert isinstance(text, str)
    combined = _joined(pages)
    assert "Helios" in combined
    assert "$6.3" in combined, "revenue $6.3B not extractable from DOCX"
    assert "$4.1" in combined, "net debt $4.1B (docx-table value) not extractable from DOCX"
    assert "$1.9" in combined, "adjusted EBITDA $1.9B (docx-table value) not extractable from DOCX"


def test_parse_txt_verbatim_single_block(ingest_mod, samples):
    pages = ingest_mod.parse_file(samples["northwind"], ".txt")
    assert len(pages) == 1, f"TXT must parse to one verbatim block, got {len(pages)}"
    page, text = pages[0]
    assert page is None
    assert "$1.84" in text and "$1.12" in text
    # verbatim: parsed block matches the file content
    raw = samples["northwind"].read_text(encoding="utf-8", errors="replace")
    assert text.strip() == raw.strip(), "TXT parsing is not verbatim"


def test_parse_csv_windowing(ingest_mod, tmp_path):
    rows = ["region,quarter,revenue_musd"]
    rows += [f"R{i},Q2,{i}.5" for i in range(100)]
    csv_path = tmp_path / "metrics.csv"
    csv_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    pages = ingest_mod.parse_file(csv_path, ".csv")
    assert pages
    for page, text in pages:
        assert page is None
    assert 2 <= len(pages) <= 5, (
        f"100 data rows in ~40-row windows should yield 2-5 blocks, got {len(pages)}"
    )
    combined = _joined(pages)
    # rows serialized as `col: value` lines (tolerate spacing after the colon)
    compact = combined.replace(" ", "")
    assert "region:R0" in compact, "CSV rows not serialized as `col: value` lines"
    assert "revenue_musd" in combined and "R7" in combined
    assert combined.count(":") >= 100, "CSV rows do not look like `col: value` serialization"


def test_chunk_metadata_and_provenance_prefix(ingest_mod):
    doc_id = str(uuid.uuid4())
    doc_name = "sample_report.pdf"
    filler = " ".join(
        f"Sentence {i} of the sample report describes revenue growth and margin detail." for i in range(160)
    )
    nodes = ingest_mod.chunk_pages(doc_id, doc_name, [(1, filler), (2, filler)])
    assert len(nodes) >= 2, "SentenceSplitter(512/64) should split this text into multiple chunks"
    seen_ix = []
    for node in nodes:
        md = node.metadata
        for key in ("doc_id", "doc_name", "page", "chunk_ix"):
            assert key in md, f"chunk metadata missing {key!r}: {md}"
        assert md["doc_id"] == doc_id
        assert md["doc_name"] == doc_name
        assert md["page"] in (1, 2)
        assert isinstance(md["chunk_ix"], int)
        seen_ix.append(md["chunk_ix"])
        text = getattr(node, "text", None) or node.get_content()
        assert text.startswith(f"[{doc_name} — p.{md['page']}]"), (
            f"chunk text lacks provenance prefix: {text[:80]!r}"
        )
    assert len(set(seen_ix)) == len(seen_ix), f"chunk_ix values not unique: {seen_ix}"

    # page=None variant: prefix without the page part
    nodes_none = ingest_mod.chunk_pages(str(uuid.uuid4()), "notes.txt", [(None, "Alpha Detective notes.")])
    assert nodes_none
    text0 = getattr(nodes_none[0], "text", None) or nodes_none[0].get_content()
    assert text0.startswith("[notes.txt]"), f"page-None prefix wrong: {text0[:60]!r}"
    assert nodes_none[0].metadata["page"] is None


# --------------------------------------------------------------------------
# API-level ingestion behavior
# --------------------------------------------------------------------------
def test_upload_every_supported_format(stack, qa, tmp_path):
    md_path = tmp_path / "analyst_notes.md"
    md_path.write_text("# Analyst notes\nAlpha Detective markdown ingest check with revenue commentary.\n")
    csv_path = tmp_path / "kpis.csv"
    csv_path.write_text("metric,value\nrevenue_musd,48.2\nstores,214\n", encoding="utf-8")
    paths = list(stack.samples.values()) + [md_path, csv_path]
    resp = qa.upload_paths(stack.client, paths)
    assert resp.status_code == 200, resp.text[:400]
    entries = resp.json()["documents"]
    assert [e["name"] for e in entries] == [p.name for p in paths], "entries not in upload order"
    for entry in entries:
        assert entry["status"] == "indexed", f"{entry['name']}: {entry}"
        assert qa.UUID4_RE.match(entry["id"]), f"id is not UUIDv4: {entry['id']!r}"
        assert entry["chunks"] >= 1
        assert not entry.get("error")
        if entry["name"].endswith(".pdf"):
            assert isinstance(entry["pages"], int) and entry["pages"] >= 1
        else:
            assert entry["pages"] is None, f"{entry['name']}: pages must be null for non-PDF"
    listing = stack.client.get("/api/documents").json()
    assert listing["totals"]["documents"] == 5


def test_oversize_file_fails_per_file(reject_stack, qa):
    big = b"A" * (25 * MB + 1)
    resp = qa.upload_bytes(reject_stack.client, [("big.txt", big)])
    assert resp.status_code == 200, f"per-file failures keep HTTP 200; got {resp.status_code}: {resp.text[:300]}"
    entry = resp.json()["documents"][0]
    assert entry["status"] == "failed"
    assert entry["id"] is None and entry["pages"] is None and entry["chunks"] == 0
    assert isinstance(entry.get("error"), str) and entry["error"].strip()
    assert reject_stack.client.get("/api/documents").json()["totals"]["documents"] == 0


def test_more_than_twenty_files_rejected_bad_file(reject_stack, qa):
    items = [(f"cap_{i}.txt", f"Alpha Detective cap probe {i}".encode()) for i in range(21)]
    resp = qa.upload_bytes(reject_stack.client, items)
    qa.assert_error_envelope(resp, status=400, code="bad_file")
    assert reject_stack.client.get("/api/documents").json()["totals"]["documents"] == 0


def test_missing_files_field_rejected_bad_file(reject_stack, qa):
    # (a) no multipart body at all
    qa.assert_error_envelope(reject_stack.client.post("/api/documents"), status=400, code="bad_file")
    # (b) multipart present but wrong field name => `files` missing/empty
    resp = reject_stack.client.post(
        "/api/documents", files=[("attachments", ("x.txt", b"wrong field", "text/plain"))]
    )
    qa.assert_error_envelope(resp, status=400, code="bad_file")


def test_unsupported_extension_fails_per_file(reject_stack, qa):
    resp = qa.upload_bytes(reject_stack.client, [("notes.exe", b"MZ\x90\x00binary")])
    assert resp.status_code == 200, resp.text[:300]
    entry = resp.json()["documents"][0]
    assert entry["status"] == "failed"
    assert entry["id"] is None and entry["chunks"] == 0
    assert isinstance(entry.get("error"), str) and entry["error"].strip()
    assert reject_stack.client.get("/api/documents").json()["totals"]["documents"] == 0


def test_content_extension_mismatch_fails_per_file(reject_stack, qa):
    # named .pdf, but not %PDF magic bytes -> sniff must reject
    resp = qa.upload_bytes(reject_stack.client, [("fake.pdf", b"just plain text pretending")])
    assert resp.status_code == 200, resp.text[:300]
    entry = resp.json()["documents"][0]
    assert entry["status"] == "failed", "magic-byte sniff did not reject a fake .pdf"
    assert entry["id"] is None
    assert isinstance(entry.get("error"), str) and entry["error"].strip()


def test_empty_file_fails_per_file(reject_stack, qa):
    resp = qa.upload_bytes(reject_stack.client, [("empty.txt", b"")])
    assert resp.status_code == 200, resp.text[:300]
    entry = resp.json()["documents"][0]
    assert entry["status"] == "failed", "empty file must fail (extracted text non-empty rule)"
    assert entry["id"] is None and entry["chunks"] == 0


def test_duplicate_upload_no_reindex(stack, qa):
    first = qa.upload_paths(stack.client, [stack.samples["meridian"]]).json()["documents"][0]
    assert first["status"] == "indexed"
    listing1 = stack.client.get("/api/documents").json()
    uploaded_at1 = {d["id"]: d["uploaded_at"] for d in listing1["documents"]}

    second = qa.upload_paths(stack.client, [stack.samples["meridian"]]).json()["documents"][0]
    assert second["status"] == "duplicate"
    assert second["id"] == first["id"], "duplicate must return the existing doc's id"
    assert second["chunks"] == first["chunks"] and second["pages"] == first["pages"]
    assert not second.get("error"), "duplicate entries carry no error field"

    # same bytes under a different filename is still the same document (sha256 of bytes)
    renamed = qa.upload_paths(stack.client, [stack.samples["meridian"]], names=["renamed_copy.pdf"]).json()[
        "documents"
    ][0]
    assert renamed["status"] == "duplicate"
    assert renamed["id"] == first["id"]
    assert renamed["name"] == first["name"], "duplicate must report the EXISTING doc's stored name"

    listing2 = stack.client.get("/api/documents").json()
    assert listing2["totals"] == listing1["totals"], "duplicate upload changed the store"
    assert {d["id"]: d["uploaded_at"] for d in listing2["documents"]} == uploaded_at1, (
        "duplicate upload re-indexed (uploaded_at changed)"
    )


def test_path_traversal_filename_sanitized(stack, qa):
    payload = b"Path traversal sanitization probe for Alpha Detective uploads."
    resp = qa.upload_bytes(stack.client, [("../../evil.txt", payload)])
    assert resp.status_code == 200, resp.text[:300]
    entry = resp.json()["documents"][0]
    assert entry["status"] == "indexed", f"valid .txt content should index: {entry}"
    name = entry["name"]
    assert name and ".." not in name and "/" not in name and "\\" not in name, (
        f"stored name not sanitized: {name!r}"
    )

    uploads_dir = (stack.storage / "uploads").resolve()
    stored = [p for p in uploads_dir.rglob("*") if p.is_file()]
    assert len(stored) == 1, f"expected exactly one stored raw file, found {stored}"
    assert stored[0].resolve().is_relative_to(uploads_dir), (
        f"raw upload escaped storage/uploads: {stored[0]}"
    )
    assert stored[0].read_bytes() == payload
    # the traversal must not have written anywhere above uploads/
    for escape in (
        stack.storage / "evil.txt",
        stack.storage.parent / "evil.txt",
        stack.storage.parent.parent / "evil.txt",
    ):
        assert not escape.exists(), f"traversal escaped to {escape}"
    # raw file lives under uploads/{doc_id}/
    assert stored[0].parent.name == entry["id"], "raw upload not stored under uploads/{doc_id}/"
