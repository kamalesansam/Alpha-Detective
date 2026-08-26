"""Document lifecycle: validate -> store raw -> parse -> chunk -> (embed) -> commit.

Per-file failures are reported per file (HTTP stays 200); request-level
violations are the API layer's job. Heavy work runs in `asyncio.to_thread`
so /api/health and /api/documents stay responsive mid-index. Ingest and
delete are serialized behind a single asyncio lock — correctness beats
parallel ingest.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import logging
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import config, providers, stores

logger = logging.getLogger("alpha.ingest")

ALLOWED_EXTS = (".pdf", ".docx", ".txt", ".md", ".csv")
MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_FILES_PER_REQUEST = 20
# Request-level ceiling for POST /api/documents: 20 files x 25 MB + multipart
# framing slack. Checked against Content-Length BEFORE any body buffering.
MAX_REQUEST_BYTES = MAX_FILES_PER_REQUEST * MAX_FILE_BYTES + 8 * 1024 * 1024
CSV_WINDOW_ROWS = 40

UUID4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

_ingest_lock = asyncio.Lock()


# --- filename & content validation -------------------------------------------

def sanitize_filename(name: str) -> str:
    """Basename only; strip separators/NULs/control chars; cap 120; never empty."""
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    base = "".join(ch for ch in base if ch not in ("\x00",) and (ord(ch) >= 32) and ch != "\x7f")
    base = base.strip().strip(".")
    if len(base) > 120:
        stem, dot, suffix = base.rpartition(".")
        if dot and 0 < len(suffix) <= 10:
            base = stem[: 120 - len(suffix) - 1].rstrip(".") + "." + suffix
        else:
            base = base[:120]
    return base or "file"


def sniff_ok(ext: str, head: bytes) -> bool:
    """Magic-byte / content sniff per CONTRACTS.md §1.3."""
    if ext == ".pdf":
        return head.startswith(b"%PDF")
    if ext == ".docx":
        return head.startswith(b"PK\x03\x04")
    if ext in (".txt", ".md", ".csv"):
        if b"\x00" in head:
            return False
        try:
            head.decode("utf-8")
        except UnicodeDecodeError:
            try:
                head.decode("latin-1")
            except UnicodeDecodeError:
                return False
        return True
    return False


def _decode_text(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


# --- parsing ------------------------------------------------------------------

def _serialize_table(rows: list[list[Optional[str]]]) -> str:
    """Aligned `label: value` rows — financial numbers live in tables and must
    be retrievable as text."""
    lines: list[str] = []
    for row in rows or []:
        cells = [(c if c is not None else "").strip().replace("\n", " ") for c in row]
        cells = [c for c in cells if c]
        if not cells:
            continue
        if len(cells) == 1:
            lines.append(cells[0])
        else:
            lines.append(f"{cells[0]}: {' | '.join(cells[1:])}")
    return "\n".join(lines)


def _parse_pdf(path: Path) -> list[tuple[Optional[int], str]]:
    from pypdf import PdfReader
    import pdfplumber

    reader = PdfReader(str(path))
    page_texts = [(page.extract_text() or "") for page in reader.pages]

    table_texts: list[str] = ["" for _ in page_texts]
    try:
        with pdfplumber.open(str(path)) as pdf:
            for i, page in enumerate(pdf.pages[: len(page_texts)]):
                serialized = [
                    _serialize_table(t) for t in (page.extract_tables() or []) if t
                ]
                table_texts[i] = "\n".join(s for s in serialized if s)
    except Exception:  # noqa: BLE001 — tables are additive; page text still stands
        logger.warning("pdfplumber table extraction failed; continuing with text only")

    out: list[tuple[Optional[int], str]] = []
    for i, (text, tables) in enumerate(zip(page_texts, table_texts), start=1):
        combined = text.strip()
        if tables:
            combined = (combined + "\n" + tables).strip()
        out.append((i, combined))
    return out


def _parse_docx(path: Path) -> list[tuple[Optional[int], str]]:
    import docx

    document = docx.Document(str(path))
    parts = [p.text for p in document.paragraphs if p.text and p.text.strip()]
    for table in document.tables:
        rows = [[cell.text for cell in row.cells] for row in table.rows]
        serialized = _serialize_table(rows)
        if serialized:
            parts.append(serialized)
    return [(None, "\n".join(parts))]


def _parse_csv(data: bytes) -> list[tuple[Optional[int], str]]:
    text = _decode_text(data)
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return [(None, "")]
    header = [h.strip() for h in rows[0]]
    body = rows[1:] if len(rows) > 1 else []
    if not body:  # header-only (or single-row) file: keep it retrievable verbatim
        return [(None, " | ".join(h for h in header if h))]
    blocks: list[tuple[Optional[int], str]] = []
    for start in range(0, len(body), CSV_WINDOW_ROWS):
        lines = []
        for row in body[start : start + CSV_WINDOW_ROWS]:
            pairs = []
            for j, value in enumerate(row):
                col = header[j] if j < len(header) and header[j] else f"col{j + 1}"
                value = (value or "").strip()
                if value:
                    pairs.append(f"{col}: {value}")
            if pairs:
                lines.append(" | ".join(pairs))
        blocks.append((None, "\n".join(lines)))
    return blocks


def parse_file(path: Path, ext: str) -> list[tuple[Optional[int], str]]:
    """(page, text) pairs. PDF: one pair per page (pypdf text + pdfplumber
    tables serialized as label: value rows). Non-PDF pages are None."""
    if ext == ".pdf":
        return _parse_pdf(path)
    if ext == ".docx":
        return _parse_docx(path)
    if ext == ".csv":
        return _parse_csv(path.read_bytes())
    if ext in (".txt", ".md"):
        return [(None, _decode_text(path.read_bytes()))]
    raise ValueError(f"unsupported extension {ext}")


# --- chunking -----------------------------------------------------------------

def provenance_prefix(doc_name: str, page: Optional[int]) -> str:
    return f"[{doc_name} — p.{page}] " if page is not None else f"[{doc_name}] "


def chunk_pages(doc_id: str, doc_name: str, pages: list[tuple[Optional[int], str]]) -> list:
    """SentenceSplitter(512/64) nodes; metadata {doc_id, doc_name, page, chunk_ix};
    text prefixed with provenance so the sparse index and LLM both see it."""
    from llama_index.core.node_parser import SentenceSplitter
    from llama_index.core.schema import TextNode

    splitter = SentenceSplitter(chunk_size=512, chunk_overlap=64)
    nodes: list[TextNode] = []
    chunk_ix = 0
    for page, text in pages:
        if not text or not text.strip():
            continue
        for piece in splitter.split_text(text):
            if not piece.strip():
                continue
            nodes.append(
                TextNode(
                    text=provenance_prefix(doc_name, page) + piece,
                    metadata={
                        "doc_id": doc_id,
                        "doc_name": doc_name,
                        "page": page,
                        "chunk_ix": chunk_ix,
                    },
                )
            )
            chunk_ix += 1
    return nodes


# --- ingest / delete orchestration -------------------------------------------

def _failed_entry(name: str, size: int, error: str) -> dict:
    return {
        "id": None,
        "name": name,
        "size_bytes": size,
        "pages": None,
        "chunks": 0,
        "status": "failed",
        "error": error,
    }


def _ingest_one(name: str, data: bytes) -> dict:
    """Sync pipeline for one file (runs inside asyncio.to_thread)."""
    store = stores.get_store()
    sanitized = sanitize_filename(name)
    ext = Path(sanitized).suffix.lower()
    size = len(data)

    if ext not in ALLOWED_EXTS:
        return _failed_entry(
            sanitized, size,
            f"unsupported file type {ext or '(none)'} (allowed: {' '.join(ALLOWED_EXTS)})",
        )
    if size > MAX_FILE_BYTES:
        return _failed_entry(sanitized, size, "file exceeds the 25 MB limit")
    if not sniff_ok(ext, data):
        return _failed_entry(sanitized, size, "content does not match extension")

    sha = hashlib.sha256(data).hexdigest()
    existing = store.find_by_sha(sha)
    if existing is not None:
        return {
            "id": existing["id"],
            "name": existing["name"],
            "size_bytes": existing["size_bytes"],
            "pages": existing["pages"],
            "chunks": existing["chunks"],
            "status": "duplicate",
        }

    doc_id = str(uuid.uuid4())
    doc_dir = config.UPLOADS_DIR / doc_id
    doc_dir.mkdir(parents=True, exist_ok=True)
    raw_path = doc_dir / sanitized
    raw_path.write_bytes(data)

    # The manifest write inside store.add_document is the ONLY commit point.
    # Every other exit — parse failure, no text, provider/rate-limit error,
    # crash — must leave no trace on disk: `finally` removes the upload dir
    # whenever we did not commit (spec §1.3: failed uploads persist nothing).
    committed = False
    try:
        try:
            pairs = parse_file(raw_path, ext)
        except Exception as exc:  # noqa: BLE001 — parse errors are per-file failures
            # Filename-only at WARNING (never absolute paths); full detail at DEBUG.
            logger.warning(
                "parse failed for uploaded file %r (%s)", sanitized, type(exc).__name__
            )
            logger.debug("parse failure detail for %r", sanitized, exc_info=True)
            return _failed_entry(sanitized, size, "failed to parse file")

        if not any((t or "").strip() for _, t in pairs):
            return _failed_entry(sanitized, size, "no extractable text")

        nodes = chunk_pages(doc_id, sanitized, pairs)
        if not nodes:
            return _failed_entry(sanitized, size, "no extractable text")

        vectors: Optional[list[list[float]]] = None
        bundle = providers.get_bundle()
        if bundle.provider == "gemini":
            # RateLimitedError propagates: this file + the rest of the batch fail.
            vectors = providers.embed_texts_cached(
                [n.text for n in nodes], bundle.embed_model_name
            )

        pages_count = len(pairs) if ext == ".pdf" else None
        manifest_entry = {
            "id": doc_id,
            "name": sanitized,
            "ext": ext,
            "size_bytes": size,
            "sha256": sha,
            "pages": pages_count,
            "chunks": len(nodes),
            "uploaded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "status": "indexed",
        }
        store.add_document(manifest_entry, nodes, vectors)  # manifest write = commit
        committed = True
        return {
            "id": doc_id,
            "name": sanitized,
            "size_bytes": size,
            "pages": pages_count,
            "chunks": len(nodes),
            "status": "indexed",
        }
    except providers.ProviderError:
        return _failed_entry(sanitized, size, "embedding failed (provider error)")
    finally:
        if not committed:
            shutil.rmtree(doc_dir, ignore_errors=True)


async def ingest_files(uploads: list[tuple[str, bytes]]) -> list[dict]:
    """Entries in upload order; rate-limit mid-batch fails the current and all
    remaining files while already-committed ones stay indexed (HTTP stays 200)."""
    results: list[dict] = []
    async with _ingest_lock:
        rate_limited: Optional[providers.RateLimitedError] = None
        for name, data in uploads:
            if rate_limited is not None:
                results.append(
                    _failed_entry(
                        sanitize_filename(name), len(data),
                        f"rate_limited: retry in ~{rate_limited.retry_after_s}s",
                    )
                )
                continue
            try:
                results.append(await asyncio.to_thread(_ingest_one, name, data))
            except providers.RateLimitedError as exc:
                rate_limited = exc
                results.append(
                    _failed_entry(
                        sanitize_filename(name), len(data),
                        f"rate_limited: retry in ~{exc.retry_after_s}s",
                    )
                )
    return results


async def delete_document(doc_id: str) -> bool:
    """False (=> API 404) unless doc_id is a well-formed uuid4 present in the
    manifest; uuid validation happens BEFORE any store/filesystem access."""
    if not UUID4_RE.match(doc_id or ""):
        return False
    async with _ingest_lock:
        store = stores.get_store()
        if store.find_by_id(doc_id) is None:
            return False
        await asyncio.to_thread(store.delete_document, doc_id)
        return True


# --- startup backfill (§3.5) --------------------------------------------------

def backfill_missing_embeddings() -> tuple[int, int]:
    """Gemini mode: embed docstore nodes for any manifest doc with Chroma count
    0 (indexed keyless earlier). Cache-first, batched. Rate-limit => warn and
    stop; the doc stays at count 0 (BM25 still covers it) until next restart."""
    bundle = providers.get_bundle()
    if bundle.provider != "gemini":
        return 0, 0
    store = stores.get_store()
    docs = chunks = 0
    for entry in store.get_manifest():
        if store.chroma_count_for(entry["id"]) != 0:
            continue
        nodes = store.nodes_for([entry["id"]])
        if not nodes:
            continue
        try:
            vectors = providers.embed_texts_cached(
                [n.text for n in nodes], bundle.embed_model_name
            )
        except providers.RateLimitedError as exc:
            logger.warning(
                "backfill rate-limited (retry in ~%ss) — continuing to serve; "
                "dense retrieval lacks the remaining docs until next restart",
                exc.retry_after_s,
            )
            break
        except providers.ProviderError:
            logger.warning("backfill failed (provider error) — continuing to serve")
            break
        store.add_vectors(entry["id"], nodes, vectors)
        docs += 1
        chunks += len(nodes)
    if docs:
        logger.info("backfilled %d docs / %d chunks", docs, chunks)
    return docs, chunks
