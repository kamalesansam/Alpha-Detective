"""Persistence only: Chroma + SimpleDocumentStore (BM25 corpus) + manifest.

Consistency rules (CONTRACTS.md §3):
- manifest.json is the source of truth; writes are atomic (tmp + os.replace).
- Ingest commit point = manifest, written LAST. Delete commit point = manifest,
  written FIRST. Both crash windows therefore leave only orphans (ids present
  in Chroma/docstore/uploads but not in manifest), which reconcile() purges
  deterministically at startup.
- Any OTHER disagreement fails loud (StoreCorruptionError -> CRITICAL + exit 1).
  Indexed state is never silently rebuilt: a guessed rebuild can serve wrong
  answers, and accuracy is the product.

No retrieval logic lives here. `epoch` increments on every mutation; retrieval
keys its BM25 cache on it (no stores -> retrieval import).
"""

from __future__ import annotations

import copy
import json
import logging
import os
import shutil
from typing import Any, Optional

from . import config

logger = logging.getLogger("alpha.stores")

COLLECTION_NAME = "chunks"


class StoreCorruptionError(Exception):
    """Unexplainable store disagreement. Startup must CRITICAL-log and exit 1."""


def _atomic_write_json(path, payload: Any) -> None:
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


class StoreManager:
    """Singleton over the three stores. Mutations are serialized by ingest's lock."""

    def __init__(self) -> None:
        self._client = None
        self._collection = None
        self._docstore = None
        self._manifest: list[dict] = []
        self.epoch: int = 0
        self._loaded = False

    # ------------------------------------------------------------------ load

    def load(self) -> None:
        """Open/create every store, then reconcile()."""
        config.ensure_storage_dirs()

        # Chroma — cosine space is mandatory (never the default L2).
        try:
            import chromadb

            self._client = chromadb.PersistentClient(
                path=str(config.CHROMA_DIR),
                settings=chromadb.Settings(anonymized_telemetry=False, allow_reset=False),
            )
            self._collection = self._client.get_or_create_collection(
                name=COLLECTION_NAME, metadata={"hnsw:space": "cosine"}
            )
        except Exception as exc:  # noqa: BLE001
            raise StoreCorruptionError(
                f"chroma store unusable ({type(exc).__name__}); "
                "remediation: delete backend/storage/ and re-upload, or restore from backup"
            ) from exc

        # Manifest — missing => fresh; unparseable => fail loud.
        if config.MANIFEST_PATH.exists():
            try:
                data = json.loads(config.MANIFEST_PATH.read_text(encoding="utf-8"))
                docs = data.get("documents")
                if not isinstance(docs, list):
                    raise ValueError("manifest has no 'documents' list")
                self._manifest = docs
            except Exception as exc:  # noqa: BLE001
                raise StoreCorruptionError(
                    "manifest.json is unparseable; remediation: delete backend/storage/ "
                    "and re-upload, or restore from backup"
                ) from exc
        else:
            self._manifest = []

        # Docstore — missing => fresh; unparseable => fresh iff nothing indexed, else fail loud.
        from llama_index.core.storage.docstore import SimpleDocumentStore

        if config.DOCSTORE_PATH.exists():
            try:
                self._docstore = SimpleDocumentStore.from_persist_path(str(config.DOCSTORE_PATH))
            except Exception as exc:  # noqa: BLE001
                if not self._manifest:
                    logger.warning("docstore.json unreadable with empty manifest — recreating fresh")
                    self._docstore = SimpleDocumentStore()
                    self._persist_docstore()
                else:
                    raise StoreCorruptionError(
                        "docstore.json is unparseable but the manifest lists indexed documents; "
                        "remediation: delete backend/storage/ and re-upload, or restore from backup"
                    ) from exc
        else:
            if self._manifest:
                raise StoreCorruptionError(
                    "docstore.json missing but the manifest lists indexed documents; "
                    "remediation: delete backend/storage/ and re-upload, or restore from backup"
                )
            self._docstore = SimpleDocumentStore()

        self._loaded = True
        self.reconcile()

    # ------------------------------------------------------------- reconcile

    def _chroma_ids_by_doc(self) -> dict[str, list[str]]:
        got = self._collection.get(include=["metadatas"])
        out: dict[str, list[str]] = {}
        for node_id, md in zip(got.get("ids") or [], got.get("metadatas") or []):
            doc_id = (md or {}).get("doc_id")
            if doc_id is not None:
                out.setdefault(str(doc_id), []).append(node_id)
            else:
                out.setdefault("__unknown__", []).append(node_id)
        return out

    def _docstore_nodes_by_doc(self) -> dict[str, list[Any]]:
        out: dict[str, list[Any]] = {}
        for node in self._docstore.docs.values():
            doc_id = (node.metadata or {}).get("doc_id")
            out.setdefault(str(doc_id) if doc_id is not None else "__unknown__", []).append(node)
        return out

    def reconcile(self) -> None:
        """Deterministic orphan purge for the two known crash windows, then
        verify the §3.3 invariant; anything else raises StoreCorruptionError."""
        manifest_ids = {d["id"] for d in self._manifest}
        purged = False

        # 3.4(3) — orphan purge: ids present in a store but NOT in the manifest.
        chroma_by_doc = self._chroma_ids_by_doc()
        for doc_id, node_ids in chroma_by_doc.items():
            if doc_id not in manifest_ids:
                self._collection.delete(ids=node_ids)
                logger.warning("reconcile: purged %d orphan chroma rows for doc_id=%s", len(node_ids), doc_id)
                purged = True

        ds_by_doc = self._docstore_nodes_by_doc()
        for doc_id, nodes in ds_by_doc.items():
            if doc_id not in manifest_ids:
                for node in nodes:
                    self._docstore.delete_document(node.node_id, raise_error=False)
                logger.warning("reconcile: purged %d orphan docstore nodes for doc_id=%s", len(nodes), doc_id)
                purged = True
        if purged:
            self._persist_docstore()

        if config.UPLOADS_DIR.exists():
            for child in config.UPLOADS_DIR.iterdir():
                if child.is_dir() and child.name not in manifest_ids:
                    shutil.rmtree(child, ignore_errors=True)
                    logger.warning("reconcile: purged orphan upload dir for doc_id=%s", child.name)
                    purged = True

        if purged:
            self.epoch += 1
            chroma_by_doc = self._chroma_ids_by_doc()
            ds_by_doc = self._docstore_nodes_by_doc()

        # 3.4(4) — verify the per-doc invariant; fail loud on any violation.
        problems: list[str] = []
        for entry in self._manifest:
            doc_id, chunks = entry["id"], int(entry["chunks"])
            ds_count = len(ds_by_doc.get(doc_id, []))
            if ds_count != chunks:
                problems.append(f"doc_id={doc_id}: docstore has {ds_count} chunks, manifest says {chunks}")
            chroma_count = len(chroma_by_doc.get(doc_id, []))
            if chroma_count not in (0, chunks):  # 0 == indexed keyless (mode-aware invariant)
                problems.append(f"doc_id={doc_id}: chroma has {chroma_count} vectors, expected 0 or {chunks}")
            doc_dir = config.UPLOADS_DIR / doc_id
            files = [p for p in doc_dir.iterdir() if p.is_file()] if doc_dir.is_dir() else []
            if not files:
                problems.append(f"doc_id={doc_id}: raw upload file missing under uploads/")
            elif len(files) > 1:
                logger.warning("reconcile: uploads/%s holds %d files (expected 1)", doc_id, len(files))
        if "__unknown__" in chroma_by_doc:
            problems.append(f"chroma holds {len(chroma_by_doc['__unknown__'])} rows without a doc_id")
        if "__unknown__" in ds_by_doc:
            problems.append(f"docstore holds {len(ds_by_doc['__unknown__'])} nodes without a doc_id")

        if problems:
            raise StoreCorruptionError(
                "store consistency check failed: "
                + "; ".join(problems)
                + " — remediation: delete backend/storage/ and re-upload, or restore from backup"
            )

    # ------------------------------------------------------------ mutations

    def _persist_docstore(self) -> None:
        self._docstore.persist(persist_path=str(config.DOCSTORE_PATH))

    def _persist_manifest(self) -> None:
        _atomic_write_json(config.MANIFEST_PATH, {"documents": self._manifest})

    def _chroma_add(self, nodes: list, vectors: list[list[float]]) -> None:
        """Add nodes with llama-index-compatible metadata (dense retrieval can
        reconstruct full nodes). None-valued metadata (page) is omitted —
        Chroma rejects None values; a missing key reads back as null."""
        from llama_index.vector_stores.chroma import ChromaVectorStore

        prepared = []
        for node, vec in zip(nodes, vectors):
            n = copy.deepcopy(node)
            n.metadata = {k: v for k, v in (n.metadata or {}).items() if v is not None}
            n.embedding = [float(x) for x in vec]
            prepared.append(n)
        ChromaVectorStore(chroma_collection=self._collection).add(prepared)

    def add_document(self, entry: dict, nodes: list, vectors: Optional[list[list[float]]]) -> None:
        """Ingest ordering — Chroma add (when vectors) -> docstore persist ->
        manifest append LAST (the commit point)."""
        if vectors is not None:
            self._chroma_add(nodes, vectors)
        self._docstore.add_documents(nodes, allow_update=True)
        self._persist_docstore()
        self._manifest.append(entry)
        self._persist_manifest()
        self.epoch += 1

    def add_vectors(self, doc_id: str, nodes: list, vectors: list[list[float]]) -> None:
        """Backfill path (§3.5): insert embeddings for an already-indexed doc."""
        self._chroma_add(nodes, vectors)
        self.epoch += 1

    def delete_document(self, doc_id: str) -> None:
        """Delete ordering — manifest rewrite FIRST (the commit point) ->
        Chroma delete -> docstore removal + persist -> uploads removal."""
        self._manifest = [d for d in self._manifest if d["id"] != doc_id]
        self._persist_manifest()
        self._collection.delete(where={"doc_id": doc_id})
        for node in list(self._docstore.docs.values()):
            if (node.metadata or {}).get("doc_id") == doc_id:
                self._docstore.delete_document(node.node_id, raise_error=False)
        self._persist_docstore()
        shutil.rmtree(config.UPLOADS_DIR / doc_id, ignore_errors=True)
        self.epoch += 1

    # -------------------------------------------------------------- queries

    def find_by_sha(self, sha256: str) -> Optional[dict]:
        return next((d for d in self._manifest if d.get("sha256") == sha256), None)

    def find_by_id(self, doc_id: str) -> Optional[dict]:
        return next((d for d in self._manifest if d["id"] == doc_id), None)

    def get_manifest(self) -> list[dict]:
        return [dict(d) for d in self._manifest]

    def counts(self) -> tuple[int, int, int]:
        docs = len(self._manifest)
        chunks = sum(int(d.get("chunks") or 0) for d in self._manifest)
        pages = sum(int(d["pages"]) for d in self._manifest if d.get("pages") is not None)
        return docs, chunks, pages

    def nodes_for(self, doc_ids: Optional[list[str]] = None) -> list:
        """Docstore TextNodes, deterministically ordered (upload order, chunk_ix)."""
        wanted = set(doc_ids) if doc_ids else None
        order = {d["id"]: i for i, d in enumerate(self._manifest)}
        nodes = [
            n
            for n in self._docstore.docs.values()
            if wanted is None or (n.metadata or {}).get("doc_id") in wanted
        ]
        nodes.sort(
            key=lambda n: (
                order.get((n.metadata or {}).get("doc_id"), 1 << 30),
                int((n.metadata or {}).get("chunk_ix") or 0),
            )
        )
        return nodes

    def chroma_count_for(self, doc_id: str) -> int:
        got = self._collection.get(where={"doc_id": doc_id}, include=[])
        return len(got.get("ids") or [])

    def chroma_ok(self) -> bool:
        try:
            self._collection.count()
            return True
        except Exception:  # noqa: BLE001
            return False

    @property
    def collection(self):
        return self._collection


_store: Optional[StoreManager] = None


def init_store() -> StoreManager:
    """Build + load the singleton (called from startup)."""
    global _store
    _store = StoreManager()
    _store.load()
    return _store


def get_store() -> StoreManager:
    """Singleton accessor; lazily loads for callers that skip the lifespan."""
    global _store
    if _store is None or not _store._loaded:
        return init_store()
    return _store
