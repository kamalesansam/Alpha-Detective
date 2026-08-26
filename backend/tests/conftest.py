"""Shared pytest infrastructure for the Alpha Detective backend suite.

Owned by qa-engineer. Every test here is written against the binding contract
(docs/build/CONTRACTS.md) and CLAUDE_CODE_PROMPT.md SS6-SS7 -- NOT against the
implementation. All suites run keyless: GOOGLE_API_KEY is removed from the
process environment at collection time and stashed privately; only
test_grounding_live.py ever gets it back (and auto-skips when it is absent).

Mechanics
---------
* Temp storage per app instance, never the real backend/storage. CONTRACTS.md
  fixes the storage paths as *code constants* in backend.app.config
  (STORAGE_DIR, UPLOADS_DIR, CHROMA_DIR, DOCSTORE_PATH, MANIFEST_PATH,
  EMBED_CACHE_PATH) with no env override, so the harness re-imports
  backend.app.* fresh for each app instance and patches those constants --
  on config first, then on any module that did `from .config import X` --
  before create_app() runs. A sanity assert verifies the redirection held.
* app_client() yields a fastapi TestClient inside the app lifespan (the
  CONTRACTS.md SS2 startup sequence runs on __enter__, shutdown on __exit__).
* Re-building with the same storage dir after purge_backend_modules()
  simulates a process restart (test_persistence.py). Chroma's per-path
  system cache is cleared best-effort so the rebuild re-opens from disk.
"""

import contextlib
import importlib
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

# --- repo geography -------------------------------------------------------
TESTS_DIR = Path(__file__).resolve().parent
BACKEND_DIR = TESTS_DIR.parent
REPO_ROOT = BACKEND_DIR.parent
SAMPLE_DATA_DIR = BACKEND_DIR / "sample_data"
MAKE_SAMPLES = BACKEND_DIR / "scripts" / "make_samples.py"
REAL_STORAGE_DIR = BACKEND_DIR / "storage"  # must never be touched by tests

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# --- SS3.6 test isolation: snapshot the real storage at COLLECTION time ---
# CONTRACTS.md SS3.6 ratifies exactly one exception to "tests never touch the real
# backend/storage/": RERANK_MODEL_DIR (`models/`), a third-party download cache.
# The exception is fenced by a QA-owned guard (tests/test_zz_storage_isolation.py)
# that needs to know what was there BEFORE the suite ran, so a developer's local
# dev store is never mistaken for a leak -- and so a leak is never excused by one.
def storage_signature():
    """name -> (kind, subtree entry count, max subtree mtime_ns) for each direct
    child of the real backend/storage/. Never creates anything."""
    sig = {}
    if not REAL_STORAGE_DIR.is_dir():
        return sig
    for child in REAL_STORAGE_DIR.iterdir():
        try:
            if child.is_dir():
                items = list(child.rglob("*"))
                mtimes = [child.stat().st_mtime_ns] + [i.stat().st_mtime_ns for i in items]
                sig[child.name] = ("dir", len(items), max(mtimes))
            else:
                st = child.stat()
                sig[child.name] = ("file", st.st_size, st.st_mtime_ns)
        except OSError:
            sig[child.name] = ("unreadable", 0, 0)
    return sig


REAL_STORAGE_AT_COLLECTION = storage_signature()

# --- SS3.6 layer 1: ATTRIBUTABLE sandbox proof -----------------------------
# The tree diff below (layer 2) can tell that backend/storage changed, but never
# WHO changed it -- a dev server writes there legitimately while `make test` runs.
# This layer is attributable and has no false positives: every app build proves
# that each redirectable path constant resolved inside its temp dir and outside
# the real store. If the suite cannot reach real storage, it cannot have written
# to it, whatever else on the machine did.
SANDBOX_CHECKS = {"builds": 0, "paths_verified": 0}
SANDBOX_VIOLATIONS = []


def _probe_port(port, timeout=0.2):
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        try:
            return sock.connect_ex(("127.0.0.1", int(port))) == 0
        except OSError:
            return False


def external_writer_evidence(settle_s=0.75):
    """Best-effort discrimination between 'a test escaped' and 'something else on
    this machine is writing to backend/storage right now'. Returns a list of
    human-readable evidence strings; empty means no external writer was found."""
    evidence = []
    first = storage_signature()
    time.sleep(settle_s)
    if storage_signature() != first:
        evidence.append(
            f"backend/storage changed again during a {settle_s}s settle window with no "
            "test running -- another process is writing to it right now"
        )
    for port in dict.fromkeys([os.environ.get("PORT") or "8000", "8000", "3000"]):
        if _probe_port(port):
            evidence.append(f"something is listening on 127.0.0.1:{port} (a dev server?)")
    if shutil.which("lsof") and REAL_STORAGE_DIR.is_dir():
        try:
            out = subprocess.run(
                ["lsof", "-t", "+D", str(REAL_STORAGE_DIR)],
                capture_output=True, text=True, timeout=5,
            ).stdout
            pids = {p for p in out.split() if p.isdigit()} - {str(os.getpid())}
            if pids:
                evidence.append(f"other process(es) hold files under backend/storage: {sorted(pids)}")
        except Exception:
            pass
    return evidence


# --- keyless by construction ---------------------------------------------
# Stash any real key at collection time; the process env stays keyless for
# every suite. Only the live grounding suite reads the stash back.
_LIVE_GOOGLE_API_KEY = os.environ.pop("GOOGLE_API_KEY", "") or ""


def live_google_api_key():
    return _LIVE_GOOGLE_API_KEY


# --- contract constants ---------------------------------------------------
REFUSAL_SENTENCE = "The uploaded documents don't contain this information."
# CONTRACTS.md v1.2 SS1.1: the enum goes 6 -> 7 and no further. `unauthorized`
# (401) is the ACCESS_CODE gate; the local throttle deliberately REUSES
# `rate_limited` rather than inventing `throttled`.
ERROR_CODES = {
    "bad_request",
    "bad_file",
    "unauthorized",
    "not_found",
    "rate_limited",
    "provider_error",
    "internal",
}
UUID4_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.I)
ISO_Z_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")

SAMPLE_FILENAMES = {
    "meridian": "meridian_q2_fy2026_earnings_call.pdf",
    "northwind": "northwind_retail_q2_2026_earnings.txt",
    "helios": "helios_energy_fy2025_annual_report.docx",
}

CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    # new in v1.2 (CONTRACTS.md SS1.3 ALLOWED_EXTS)
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".html": "text/html",
    ".htm": "text/html",
    ".json": "application/json",
}

# CONTRACTS.md SS1.3 -- the full v1.2 extension list, in contract order.
ALLOWED_EXTS_V12 = (
    ".pdf", ".docx", ".txt", ".md", ".csv", ".xlsx", ".pptx", ".html", ".htm", ".json",
)

# CONTRACTS.md SS1.3 "Frozen per-file `error` strings" -- QA asserts verbatim.
ERR_UNSUPPORTED = (
    "unsupported file type {ext} (allowed: " + " ".join(ALLOWED_EXTS_V12) + ")"
)
ERR_TOO_BIG = "file exceeds the 25 MB limit"
ERR_SNIFF = "content does not match extension"
ERR_PARSE = "failed to parse file"
ERR_NO_TEXT = "no extractable text"
ERR_ZIP_BOMB = "archive expands too much (possible zip bomb)"
ERR_XLSX_CELLS = "spreadsheet too large (cap: {cap} cells)"
ERR_XLSX_SHEETS = "spreadsheet has too many sheets (cap: {cap})"
ERR_PPTX_SLIDES = "presentation too large (cap: {cap} slides)"
ERR_TEXT_CAP = "extracted text too large (cap: {cap} characters)"
ERR_JSON_DEPTH = "json too deeply nested (cap: depth {cap})"
ERR_JSON_NODES = "json too large (cap: {cap} nodes)"
ERR_CORPUS_FULL = "corpus is full ({cap} documents) — delete a document first"

# CONTRACTS.md SS2 -- frozen extraction-cap constant names and values.
EXTRACTION_CAPS = {
    "OOXML_MAX_ENTRIES": 5000,
    "OOXML_MAX_UNCOMPRESSED_BYTES": 100 * 1024 * 1024,  # lowered from 200 MiB, r3
    "OOXML_MAX_COMPRESSION_RATIO": 200,
    "XLSX_MAX_SHEETS": 50,
    "XLSX_MAX_CELLS": 200_000,
    "XLSX_WINDOW_ROWS": 40,
    "PPTX_MAX_SLIDES": 500,
    "PPTX_MAX_TABLE_CELLS": 20_000,
    # r3: the format-specific html cap was folded into this one, same value.
    "MAX_EXTRACTED_TEXT_CHARS": 5_000_000,
    "JSON_MAX_DEPTH": 20,
    "JSON_MAX_NODES": 200_000,
    "JSON_WINDOW_LINES": 40,
}

# CONTRACTS.md SS1.9.3 display caps.
EXPLAIN_CAPS = {"bm25": 8, "dense": 8, "fusion": 12, "rerank": 6}
# CONTRACTS.md SS1.9.4 frozen guardrail check names.
GUARDRAIL_CHECKS = {
    "nonempty",
    "rerank_floor",
    "term_overlap",
    "bm25_nonzero",
    "entity_presence",
    "period_presence",
    "exclusive_topic",
}

# CONTRACTS.md SS2 config.py path constants and their layout under STORAGE_DIR (SS3.1).
_PATH_LAYOUT = {
    "STORAGE_DIR": ".",
    "UPLOADS_DIR": "uploads",
    "CHROMA_DIR": "chroma",
    "DOCSTORE_PATH": "docstore.json",
    "MANIFEST_PATH": "manifest.json",
    "EMBED_CACHE_PATH": "embed_cache.json",
}

# v1.2 adds derived-data paths under STORAGE_DIR (SS2 config.py / SS3.1). They are
# NOT part of the six frozen names, so they are redirected only when they exist --
# but redirected they must be: a test that writes the real
# backend/storage/llm_budget.json has escaped its sandbox.
#
# Deliberately NOT redirected: SAMPLE_DATA_DIR (read-only seed corpus under
# backend/, not storage) and RERANK_MODEL_DIR (a pure download cache -- pointing
# it at tmp would re-download the cross-encoder for every RERANK=on app build and
# make the suite network-dependent).
_EXTRA_PATH_LAYOUT = {
    "LLM_BUDGET_PATH": "llm_budget.json",
}

# Default env for every keyless app build. RERANK defaults off here so that
# non-gate suites are deterministic and never wait on a model download; the
# accuracy gate explicitly runs both RERANK=on and RERANK=off.
# --- hermetic environment (SS5) -------------------------------------------
# EVERY variable in the SS5 matrix is pinned explicitly, because anything left
# unpinned is read from the developer's `backend/.env` (or an inherited process
# env) and silently reconfigures the app under test. That is not hypothetical:
# a `make dev` session that set `ACCESS_CODE=demo1234` for a screenshot turned
# this suite into 132 failures / 221 errors of `401 unauthorized`, and the
# failures looked like product bugs. The harness owns the configuration of the
# app it builds; a test run must mean the same thing on every machine.
#
# Deliberate non-default choices, each exercised explicitly elsewhere:
#   PROVIDER=none        -> keyless by construction (test_env_hygiene covers auto/gemini)
#   RERANK=off           -> deterministic, no model download (the eval gate runs both)
#   AUTO_SEED=off        -> the harness owns the corpus (test_rails covers on/off)
#   RATE_LIMIT_PER_MIN=0 -> the 30-case eval gate issues >10 queries (test_rails
#                           covers the shipped default posture end to end)
HERMETIC_ENV = {
    "GOOGLE_API_KEY": None,
    "PROVIDER": "none",
    "GEMINI_LLM_MODEL": "auto",
    "GEMINI_EMBED_MODEL": "auto",
    "RERANK": "off",
    "PORT": None,
    "CORS_ORIGINS": "http://localhost:3000",
    "ACCESS_CODE": "",
    "DAILY_LLM_BUDGET": "200",
    "RATE_LIMIT_PER_MIN": "0",
    "MAX_DOCUMENTS": "50",
    "AUTO_SEED": "off",
    "TRUSTED_PROXY_HOPS": "0",
}

_BASE_ENV = dict(HERMETIC_ENV)


# --- module lifecycle -----------------------------------------------------
def purge_backend_modules():
    """Drop every backend.* module so the next import is fresh (a 'restart')."""
    for name in [n for n in list(sys.modules) if n == "backend" or n.startswith("backend.")]:
        sys.modules.pop(name, None)
    # Chroma caches one system per path; clear it so a re-open reads from disk.
    try:
        from chromadb.api.client import SharedSystemClient

        SharedSystemClient.clear_system_cache()
    except Exception:
        pass


def _patch_path_constants(module, storage_dir, force):
    for name, rel in _PATH_LAYOUT.items():
        target = storage_dir if rel == "." else storage_dir / rel
        if force or hasattr(module, name):
            current = getattr(module, name, None)
            setattr(module, name, str(target) if isinstance(current, str) else target)
    # v1.2 derived paths: redirect only what actually exists (never invent a seam).
    for name, rel in _EXTRA_PATH_LAYOUT.items():
        if hasattr(module, name):
            current = getattr(module, name)
            target = storage_dir / rel
            setattr(module, name, str(target) if isinstance(current, str) else target)


def load_backend(storage_dir):
    """Freshly import backend.app.* with storage redirected to storage_dir.

    Returns the backend.app.main module (which owns create_app()).
    """
    storage_dir = Path(storage_dir)
    storage_dir.mkdir(parents=True, exist_ok=True)
    assert storage_dir.resolve() != REAL_STORAGE_DIR.resolve(), (
        "tests must never run against the real backend/storage"
    )
    purge_backend_modules()
    config = importlib.import_module("backend.app.config")
    _patch_path_constants(config, storage_dir, force=True)
    for fn_name in ("get_settings",):
        fn = getattr(config, fn_name, None)
        if fn is not None and hasattr(fn, "cache_clear"):
            fn.cache_clear()
    main = importlib.import_module("backend.app.main")
    for name, mod in list(sys.modules.items()):
        if name.startswith("backend.app") and mod is not None and mod is not config:
            _patch_path_constants(mod, storage_dir, force=False)
    got = Path(str(getattr(config, "STORAGE_DIR", storage_dir))).resolve()
    assert got == storage_dir.resolve(), (
        f"storage redirection failed: config.STORAGE_DIR={got}, wanted {storage_dir.resolve()}"
    )
    real = REAL_STORAGE_DIR.resolve()
    for name in list(_PATH_LAYOUT) + list(_EXTRA_PATH_LAYOUT):
        raw = getattr(config, name, None)
        if raw is None:
            continue
        resolved = Path(str(raw)).resolve()
        if real == resolved or real in resolved.parents:
            SANDBOX_VIOLATIONS.append(f"config.{name} -> {resolved}")
        assert real != resolved and real not in resolved.parents, (
            f"config.{name} still points inside the real backend/storage ({resolved}) -- "
            "tests must never write there (SS3.6)"
        )
        SANDBOX_CHECKS["paths_verified"] += 1
    SANDBOX_CHECKS["builds"] += 1
    return main


@contextlib.contextmanager
def _set_env(mapping):
    saved = {}
    try:
        for key, value in mapping.items():
            saved[key] = os.environ.get(key)
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = str(value)
        yield
    finally:
        for key, old in saved.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


# Modules belonging to each *currently live* app, innermost last. A function-scoped
# app_client nested inside a module-scoped one purges sys.modules on exit, which
# would otherwise strand the outer app: its code objects keep running while
# `backend.app.*` resolves to nothing (or, worse, re-imports with unpatched paths).
_LIVE_APP_MODULES = []


def live_backend_modules():
    """name -> module for the innermost live app; falls back to sys.modules."""
    if _LIVE_APP_MODULES:
        return dict(_LIVE_APP_MODULES[-1])
    return {n: m for n, m in sys.modules.items() if n.startswith("backend.app") and m}


@contextlib.contextmanager
def app_client(storage_dir, env=None):
    """Build a fresh app against storage_dir and yield a lifespan-wrapped TestClient."""
    merged = dict(_BASE_ENV)
    merged.update(env or {})
    # Keyless unless the caller (live suite only) explicitly supplies a key.
    merged.setdefault("GOOGLE_API_KEY", None)
    with _set_env(merged):
        main = load_backend(storage_dir)
        _LIVE_APP_MODULES.append(
            {n: m for n, m in sys.modules.items() if n.startswith("backend") and m is not None}
        )
        app = main.create_app()
        from fastapi.testclient import TestClient

        depth = len(_LIVE_APP_MODULES)
        try:
            with TestClient(app) as client:
                yield client
        finally:
            # Balance assertion: an app that is popped at a different depth than it
            # was pushed means two app lifetimes interleaved, which is exactly how
            # one test's live modules end up serving another test's requests.
            assert len(_LIVE_APP_MODULES) == depth, (
                f"app_client stack imbalance: pushed at depth {depth - 1}, popping at "
                f"{len(_LIVE_APP_MODULES) - 1} -- app lifetimes interleaved"
            )
            _LIVE_APP_MODULES.pop()
            purge_backend_modules()
            if _LIVE_APP_MODULES:
                sys.modules.update(_LIVE_APP_MODULES[-1])


# --- API helpers ----------------------------------------------------------
def content_type_for(name):
    return CONTENT_TYPES.get(Path(name).suffix.lower(), "application/octet-stream")


def upload_bytes(client, items):
    """items: list of (filename, bytes). Multipart field name per contract: `files`."""
    files = [("files", (name, data, content_type_for(name))) for name, data in items]
    return client.post("/api/documents", files=files)


def upload_paths(client, paths, names=None):
    items = []
    for i, p in enumerate(paths):
        p = Path(p)
        items.append((names[i] if names else p.name, p.read_bytes()))
    return upload_bytes(client, items)


def index_samples(client, samples):
    """Upload the three sample docs, assert all indexed, return {name: entry}."""
    resp = upload_paths(client, list(samples.values()))
    assert resp.status_code == 200, f"sample upload failed: HTTP {resp.status_code}: {resp.text[:500]}"
    out = {}
    for entry in resp.json()["documents"]:
        assert entry["status"] == "indexed", f"sample {entry.get('name')!r} not indexed: {entry}"
        assert entry["chunks"] >= 1, f"indexed sample with zero chunks: {entry}"
        out[entry["name"]] = entry
    assert set(out) == set(SAMPLE_FILENAMES.values()), f"unexpected sample names: {sorted(out)}"
    return out


def post_query(client, question, doc_ids=None, top_k=None, **extra):
    payload = {"question": question}
    if doc_ids is not None:
        payload["doc_ids"] = doc_ids
    if top_k is not None:
        payload["top_k"] = top_k
    payload.update(extra)
    return client.post("/api/query", json=payload)


def assert_error_envelope(resp, status=None, code=None):
    """Assert the CONTRACTS.md SS1.1 envelope: shape, code, and zero leakage."""
    if status is not None:
        assert resp.status_code == status, (
            f"expected HTTP {status}, got {resp.status_code}: {resp.text[:400]}"
        )
    assert resp.status_code >= 400, f"expected an error response, got HTTP {resp.status_code}"
    try:
        body = resp.json()
    except Exception:
        raise AssertionError(f"non-JSON error body (HTTP {resp.status_code}): {resp.text[:300]}")
    assert isinstance(body, dict) and set(body.keys()) == {"error"}, (
        f"error body is not the envelope (raw 422/detail must never surface): {body}"
    )
    err = body["error"]
    assert isinstance(err, dict), f"envelope 'error' is not an object: {err}"
    assert set(err.keys()).issubset({"code", "message", "retry_after_s"}), (
        f"unexpected envelope keys: {sorted(err)}"
    )
    assert err.get("code") in ERROR_CODES, f"unknown error code: {err.get('code')!r}"
    if code is not None:
        assert err["code"] == code, f"expected code {code!r}, got {err['code']!r} ({err})"
    assert isinstance(err.get("message"), str) and err["message"].strip(), f"empty message: {err}"
    if "retry_after_s" in err:
        assert isinstance(err["retry_after_s"], int), f"retry_after_s not int: {err}"
    text = resp.text
    for leak in ("Traceback", "site-packages", str(REPO_ROOT), "/home/", "/Users/"):
        assert leak not in text, f"error response leaks internals ({leak!r}): {text[:400]}"
    return err


# --- v1.2: in-test fixture generators (tiny, self-contained, no golden files) --
def make_html(body="<p>Hello</p>", title="Doc", script="var x=1;", style="p{color:red}"):
    """A tiny HTML doc carrying script/style/comment/entity traps (SS2 ingest.py)."""
    return (
        f"<!doctype html><html><head><title>{title}</title>"
        f"<style>{style}</style><script>{script}</script></head>"
        f"<body><!-- HIDDENCOMMENT -->{body}</body></html>"
    ).encode("utf-8")


def make_json_bytes(obj):
    import json as _json

    return _json.dumps(obj).encode("utf-8")


def nested_json(depth, leaf="deepvalue"):
    """A JSON object nested exactly `depth` levels (root object counts as 1)."""
    node = leaf
    for _ in range(depth):
        node = {"k": node}
    return node


def make_xlsx(sheets):
    """sheets: {"SheetName": [[row0...], [row1...]]} -> .xlsx bytes (openpyxl)."""
    openpyxl = pytest.importorskip(
        "openpyxl", reason="CONTRACTS.md SS2 names openpyxl as the .xlsx parser"
    )
    import io

    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(title=name[:31])
        for row in rows:
            ws.append(list(row))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def make_pptx(slides):
    """slides: [{"text": [str, ...], "table": [[...], ...]|None}] -> .pptx bytes."""
    pptx = pytest.importorskip(
        "pptx", reason="CONTRACTS.md SS2 names python-pptx as the .pptx parser"
    )
    import io

    prs = pptx.Presentation()
    blank = prs.slide_layouts[6]
    for spec in slides:
        slide = prs.slides.add_slide(blank)
        top = pptx.util.Inches(0.5)
        for line in spec.get("text") or []:
            box = slide.shapes.add_textbox(
                pptx.util.Inches(0.5), top, pptx.util.Inches(8), pptx.util.Inches(0.6)
            )
            box.text_frame.text = line
            top = top + pptx.util.Inches(0.7)
        table = spec.get("table")
        if table:
            rows, cols = len(table), max(len(r) for r in table)
            shape = slide.shapes.add_table(
                rows, cols, pptx.util.Inches(0.5), top,
                pptx.util.Inches(8), pptx.util.Inches(0.4 * rows),
            )
            for r, row in enumerate(table):
                for c in range(cols):
                    shape.table.cell(r, c).text = str(row[c]) if c < len(row) else ""
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def make_zip_bytes(entries, compress=True):
    """Raw zip bytes with a PK\\x03\\x04 header -- used for OOXML container-guard tests."""
    import io
    import zipfile

    buf = io.BytesIO()
    mode = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    with zipfile.ZipFile(buf, "w", mode) as zf:
        for name, data in entries:
            zf.writestr(name, data)
    return buf.getvalue()


# --- v1.2: provider tripwire + cross-module attribute patching -------------
# CONTRACTS.md SS2 providers.py is the ONLY Gemini gateway. These are the seams
# SS1.9.1 must never touch: explain mode makes zero LLM and zero embedding calls.
PROVIDER_SEAMS_REQUIRED = ("embed_texts_cached", "complete_with_backoff", "resolve_models")
PROVIDER_SEAMS_OPTIONAL = ("_make_genai_client", "_make_llm", "_make_embed_model")


def _backend_modules():
    return [m for n, m in live_backend_modules().items() if n.startswith("backend.app")]


@contextlib.contextmanager
def patch_backend_attr(name, value, required=True):
    """Rebind `name` on EVERY loaded backend.app module that defines it.

    Necessary because modules bind provider functions both ways
    (`from . import providers` and `from .providers import get_bundle`);
    patching only the defining module would silently miss the real call site.
    """
    saved = []
    for mod in _backend_modules():
        if hasattr(mod, name):
            saved.append((mod, getattr(mod, name)))
            setattr(mod, name, value)
    if required:
        assert saved, f"no loaded backend.app module exposes {name!r} -- seam missing"
    try:
        yield saved
    finally:
        for mod, old in saved:
            setattr(mod, name, old)


@contextlib.contextmanager
def no_provider_calls(extra=()):
    """Trip on ANY Gemini-facing call. Yields the (empty) call log for assertions."""
    prov = live_backend_modules().get("backend.app.providers")
    assert prov is not None, "backend.app.providers is not live -- build the app first"
    calls = []
    names = []
    for n in PROVIDER_SEAMS_REQUIRED:
        assert hasattr(prov, n), (
            f"providers.{n} is a CONTRACTS.md SS2 seam and is missing -- "
            "QA cannot prove 'zero provider calls' without it"
        )
        names.append(n)
    names += [n for n in PROVIDER_SEAMS_OPTIONAL if hasattr(prov, n)]
    names += [n for n in extra if hasattr(prov, n)]

    def _tripwire(fn_name):
        def _boom(*a, **kw):
            calls.append(fn_name)
            raise AssertionError(
                f"providers.{fn_name} was called -- CONTRACTS.md SS1.9.1 forbids any "
                "extra LLM/embedding call (and keyless mode forbids all of them)"
            )

        return _boom

    with contextlib.ExitStack() as stack:
        for n in names:
            stack.enter_context(patch_backend_attr(n, _tripwire(n), required=False))
        yield calls


@contextlib.contextmanager
def count_provider_calls():
    """Count (but still allow) provider calls -- for budget/degradation tests."""
    prov = live_backend_modules().get("backend.app.providers")
    assert prov is not None
    counts = {}

    def _wrap(fn_name, original):
        def _counted(*a, **kw):
            counts[fn_name] = counts.get(fn_name, 0) + 1
            return original(*a, **kw)

        return _counted

    with contextlib.ExitStack() as stack:
        for n in PROVIDER_SEAMS_REQUIRED:
            if hasattr(prov, n):
                stack.enter_context(patch_backend_attr(n, _wrap(n, getattr(prov, n))))
        yield counts


def backend_module(name):
    """Live `backend.app.<name>` module for the app currently under test."""
    mod = live_backend_modules().get(f"backend.app.{name}")
    assert mod is not None, f"backend.app.{name} is not live -- build the app first"
    return mod


def require_attr(module, name, ref):
    """Fail with a contract citation rather than an AttributeError."""
    assert hasattr(module, name), (
        f"{module.__name__}.{name} is required by CONTRACTS.md {ref} and does not exist"
    )
    return getattr(module, name)


# --- fixtures -------------------------------------------------------------
@pytest.fixture(scope="session")
def qa():
    """One namespace with every contract constant + helper the suites need."""
    return SimpleNamespace(
        REFUSAL=REFUSAL_SENTENCE,
        ERROR_CODES=ERROR_CODES,
        UUID4_RE=UUID4_RE,
        ISO_Z_RE=ISO_Z_RE,
        SAMPLE_FILENAMES=dict(SAMPLE_FILENAMES),
        REPO_ROOT=REPO_ROOT,
        app_client=app_client,
        load_backend=load_backend,
        purge_backend_modules=purge_backend_modules,
        upload_bytes=upload_bytes,
        upload_paths=upload_paths,
        index_samples=index_samples,
        query=post_query,
        assert_error_envelope=assert_error_envelope,
        content_type_for=content_type_for,
        live_key=live_google_api_key(),
        # --- v1.2 ---
        ALLOWED_EXTS=ALLOWED_EXTS_V12,
        EXTRACTION_CAPS=dict(EXTRACTION_CAPS),
        EXPLAIN_CAPS=dict(EXPLAIN_CAPS),
        GUARDRAIL_CHECKS=set(GUARDRAIL_CHECKS),
        ERR=SimpleNamespace(
            unsupported=ERR_UNSUPPORTED,
            too_big=ERR_TOO_BIG,
            sniff=ERR_SNIFF,
            parse=ERR_PARSE,
            no_text=ERR_NO_TEXT,
            zip_bomb=ERR_ZIP_BOMB,
            xlsx_cells=ERR_XLSX_CELLS,
            xlsx_sheets=ERR_XLSX_SHEETS,
            pptx_slides=ERR_PPTX_SLIDES,
            text_cap=ERR_TEXT_CAP,
            json_depth=ERR_JSON_DEPTH,
            json_nodes=ERR_JSON_NODES,
            corpus_full=ERR_CORPUS_FULL,
        ),
        make_html=make_html,
        make_json_bytes=make_json_bytes,
        nested_json=nested_json,
        make_xlsx=make_xlsx,
        make_pptx=make_pptx,
        make_zip_bytes=make_zip_bytes,
        no_provider_calls=no_provider_calls,
        count_provider_calls=count_provider_calls,
        patch_backend_attr=patch_backend_attr,
        backend_module=backend_module,
        require_attr=require_attr,
        live_backend_modules=live_backend_modules,
        REAL_STORAGE_DIR=REAL_STORAGE_DIR,
        storage_signature=storage_signature,
        storage_at_collection=dict(REAL_STORAGE_AT_COLLECTION),
    )


@pytest.fixture(scope="session")
def samples():
    """The three committed sample docs; generated once via make_samples.py if absent."""
    paths = {key: SAMPLE_DATA_DIR / name for key, name in SAMPLE_FILENAMES.items()}
    if not all(p.is_file() for p in paths.values()):
        if not MAKE_SAMPLES.is_file():
            pytest.fail(
                f"sample docs missing under {SAMPLE_DATA_DIR} and generator not found at "
                f"{MAKE_SAMPLES} -- ai-engineer deliverable (`make samples`) is required first"
            )
        proc = None
        for cwd in (REPO_ROOT, BACKEND_DIR):
            proc = subprocess.run(
                [sys.executable, str(MAKE_SAMPLES)],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=300,
            )
            if all(p.is_file() for p in paths.values()):
                break
        missing = [str(p) for p in paths.values() if not p.is_file()]
        if missing:
            detail = f"\nstdout: {proc.stdout[-800:]}\nstderr: {proc.stderr[-800:]}" if proc else ""
            pytest.fail("make_samples.py did not produce: " + ", ".join(missing) + detail)
    return paths


@pytest.fixture()
def stack(tmp_path, samples, qa):
    """Function-scoped fresh, empty app (PROVIDER=none, RERANK=off) on temp storage."""
    storage = tmp_path / "storage"
    with app_client(storage) as client:
        yield SimpleNamespace(client=client, storage=storage, samples=samples)


@pytest.fixture()
def auto_stack(tmp_path, samples, qa):
    """Fresh app under PROVIDER=auto with no key (ratified r3).

    `auto` is the shipped default, so it must be exercised: keyless it has to
    resolve to retrieval-only and behave exactly like PROVIDER=none.
    """
    storage = tmp_path / "storage"
    with app_client(storage, env={"PROVIDER": "auto"}) as client:
        yield SimpleNamespace(client=client, storage=storage, samples=samples)


@pytest.fixture(scope="module")
def auto_indexed_stack(tmp_path_factory, samples, qa):
    """Module-scoped PROVIDER=auto app with the samples indexed.

    `auto` is the SHIPPED DEFAULT. The previous round's suite was green while
    the default configuration was broken because every test forced
    PROVIDER=none, so every v1.2 surface gets at least one `auto` pass here.
    """
    storage = tmp_path_factory.mktemp("auto-indexed") / "storage"
    with app_client(storage, env={"PROVIDER": "auto"}) as client:
        docs = index_samples(client, samples)
        health = client.get("/api/health").json()
        assert health["provider"] == "none", (
            f"PROVIDER=auto without a key must resolve to none (SS5): {health}"
        )
        yield SimpleNamespace(
            client=client,
            storage=storage,
            samples=samples,
            docs=docs,
            by_key={key: docs[name] for key, name in SAMPLE_FILENAMES.items()},
        )


@pytest.fixture(scope="module")
def indexed_stack(tmp_path_factory, samples, qa):
    """Module-scoped app with the three samples indexed. Read-only for its module."""
    storage = tmp_path_factory.mktemp("indexed") / "storage"
    with app_client(storage) as client:
        docs = index_samples(client, samples)
        yield SimpleNamespace(
            client=client,
            storage=storage,
            samples=samples,
            docs=docs,
            by_key={key: docs[name] for key, name in SAMPLE_FILENAMES.items()},
        )
