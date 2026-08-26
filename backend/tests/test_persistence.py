"""Restart consistency (CONTRACTS.md SS3) -- keyless, PROVIDER=none, RERANK=off.

Index the samples, dispose every app object (module purge + Chroma system-cache
clear = in-process restart), rebuild a fresh app on the SAME storage dir, and
prove: startup reconciliation passes, manifest/docstore-backed counts are
identical through the API, the on-disk manifest matches the SS3.2 schema, and
the same eval question still hits. Also proves delete consistency across a
restart (manifest-first ordering, uploads/{doc_id} removal, BM25 rebuild).

Chroma note: in keyless mode ingestion leaves Chroma untouched (SS5 matrix), so
the mode-aware invariant (SS3.3) expects a per-doc Chroma count of exactly 0;
we assert that directly on disk between the two app lifetimes. Equality of
non-zero Chroma counts across restarts is only exercisable with a key (live).
"""

import json


def _snapshot(client):
    listing = client.get("/api/documents").json()
    health = client.get("/api/health").json()
    return {
        "docs": {d["id"]: d for d in listing["documents"]},
        "totals": listing["totals"],
        "health_counts": (health["documents"], health["chunks"]),
    }


def _assert_meridian_hit(client, qa):
    resp = qa.query(client, "What was Meridian's Q2 FY2026 revenue?")
    assert resp.status_code == 200, resp.text[:300]
    body = resp.json()
    top3 = body["citations"][:3]
    assert body["no_answer"] is False, f"eval question refused after restart: {body['answer']!r}"
    meridian = qa.SAMPLE_FILENAMES["meridian"]
    assert any(c["doc_name"] == meridian for c in top3), f"meridian not in top-3: {top3}"
    assert any(c["doc_name"] == meridian and "$48.2" in c["snippet"] for c in top3), (
        f"$48.2 not in a meridian top-3 snippet: {[c['snippet'][:80] for c in top3]}"
    )


def _assert_manifest_schema(storage, qa, expected_ids):
    manifest = json.loads((storage / "manifest.json").read_text(encoding="utf-8"))
    assert set(manifest.keys()) == {"documents"}, f"manifest schema drift: {sorted(manifest)}"
    entries = {e["id"]: e for e in manifest["documents"]}
    assert set(entries) == set(expected_ids), "manifest ids disagree with the API listing"
    for entry in entries.values():
        for key in ("id", "name", "ext", "size_bytes", "sha256", "pages", "chunks", "uploaded_at", "status"):
            assert key in entry, f"manifest entry missing {key!r}: {entry}"
        assert entry["status"] == "indexed", "manifest may only hold indexed docs"
        assert entry["chunks"] >= 1
        assert isinstance(entry["sha256"], str) and len(entry["sha256"]) == 64
        assert qa.ISO_Z_RE.match(entry["uploaded_at"])
    return entries


def _chroma_count_on_disk(storage):
    """Open the persisted Chroma store directly (no app alive) and count vectors."""
    import chromadb

    client = chromadb.PersistentClient(path=str(storage / "chroma"))
    collection = client.get_collection("chunks")
    return collection.count()


def test_restart_preserves_index_and_retrieval(tmp_path, samples, qa):
    storage = tmp_path / "storage"

    # ---- lifetime 1: index and snapshot ----
    with qa.app_client(storage) as client:
        qa.index_samples(client, samples)
        before = _snapshot(client)
        _assert_meridian_hit(client, qa)
    # app fully disposed here (module purge + chroma cache clear in app_client)

    # ---- between lifetimes: on-disk truths ----
    _assert_manifest_schema(storage, qa, before["docs"].keys())
    uploads = storage / "uploads"
    assert {p.name for p in uploads.iterdir() if p.is_dir()} == set(before["docs"].keys()), (
        "uploads/ dirs disagree with manifest ids"
    )
    assert _chroma_count_on_disk(storage) == 0, (
        "keyless ingest must leave Chroma at 0 vectors (SS5 none-mode ingest)"
    )
    qa.purge_backend_modules()  # drop the direct chroma handle's cached system

    # ---- lifetime 2: rebuild from disk, same storage ----
    with qa.app_client(storage) as client:
        # startup reconciliation ran inside lifespan; reaching here means it passed
        after = _snapshot(client)
        assert after["totals"] == before["totals"], (
            f"totals changed across restart: {before['totals']} -> {after['totals']}"
        )
        assert after["health_counts"] == before["health_counts"]
        assert set(after["docs"]) == set(before["docs"]), "doc ids changed across restart"
        for doc_id, entry in before["docs"].items():
            assert after["docs"][doc_id] == entry, (
                f"doc {entry['name']} drifted across restart:\n{entry}\n{after['docs'][doc_id]}"
            )
        assert client.get("/api/health").json()["chroma_ok"] is True
        _assert_meridian_hit(client, qa)  # BM25/docstore rebuilt and still hitting


def test_delete_persists_across_restart(tmp_path, samples, qa):
    storage = tmp_path / "storage"
    meridian_name = qa.SAMPLE_FILENAMES["meridian"]

    with qa.app_client(storage) as client:
        docs = qa.index_samples(client, samples)
        meridian_id = docs[meridian_name]["id"]
        resp = client.delete(f"/api/documents/{meridian_id}")
        assert resp.status_code == 200 and resp.json() == {"ok": True}
        listing = client.get("/api/documents").json()
        assert listing["totals"]["documents"] == 2
        assert meridian_id not in {d["id"] for d in listing["documents"]}
        survivors = {d["id"] for d in listing["documents"]}

    # on-disk: manifest rewritten without the doc, raw upload dir removed
    entries = _assert_manifest_schema(storage, qa, survivors)
    assert meridian_id not in entries
    assert not (storage / "uploads" / meridian_id).exists(), "uploads/{doc_id} not removed on delete"

    with qa.app_client(storage) as client:
        listing = client.get("/api/documents").json()
        assert {d["id"] for d in listing["documents"]} == survivors
        assert listing["totals"]["documents"] == 2
        # deleted doc must be unretrievable after restart (BM25 rebuilt from docstore)
        resp = qa.query(client, "What was Meridian's Q2 FY2026 revenue?")
        assert resp.status_code == 200
        body = resp.json()
        assert all(c["doc_id"] != meridian_id for c in body["citations"]), (
            "deleted doc resurfaced in citations after restart"
        )
        assert "$48.2" not in resp.text, "deleted document's content still retrievable after restart"
