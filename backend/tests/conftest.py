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
import subprocess
import sys
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

# --- keyless by construction ---------------------------------------------
# Stash any real key at collection time; the process env stays keyless for
# every suite. Only the live grounding suite reads the stash back.
_LIVE_GOOGLE_API_KEY = os.environ.pop("GOOGLE_API_KEY", "") or ""


def live_google_api_key():
    return _LIVE_GOOGLE_API_KEY


# --- contract constants ---------------------------------------------------
REFUSAL_SENTENCE = "The uploaded documents don't contain this information."
ERROR_CODES = {"bad_request", "bad_file", "not_found", "rate_limited", "provider_error", "internal"}
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

# Default env for every keyless app build. RERANK defaults off here so that
# non-gate suites are deterministic and never wait on a model download; the
# accuracy gate explicitly runs both RERANK=on and RERANK=off.
_BASE_ENV = {"PROVIDER": "none", "RERANK": "off"}


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


@contextlib.contextmanager
def app_client(storage_dir, env=None):
    """Build a fresh app against storage_dir and yield a lifespan-wrapped TestClient."""
    merged = dict(_BASE_ENV)
    merged.update(env or {})
    # Keyless unless the caller (live suite only) explicitly supplies a key.
    merged.setdefault("GOOGLE_API_KEY", None)
    with _set_env(merged):
        main = load_backend(storage_dir)
        app = main.create_app()
        from fastapi.testclient import TestClient

        try:
            with TestClient(app) as client:
                yield client
        finally:
            purge_backend_modules()


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
