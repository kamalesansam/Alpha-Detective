"""Hybrid retrieval + the pre-LLM no-answer guardrail.

Four paths (CONTRACTS.md §5); the guardrail ALWAYS runs before any LLM call:

| PROVIDER | RERANK(eff) | Query path                                                        |
|----------|-------------|-------------------------------------------------------------------|
| gemini   | on          | dense8 + bm25-8 -> RRF pool 12 -> cross-encoder -> top_k -> floor  |
| gemini   | off         | dense8 + bm25-8 -> RRF pool 12 -> top_k by fused -> overlap check  |
| none     | on          | bm25 top-12 -> cross-encoder -> top_k -> bm25-zero/overlap check   |
| none     | off         | bm25 top-top_k -> bm25-zero/overlap check                          |

Fusion is QueryFusionRetriever(mode="reciprocal_rerank", similarity_top_k=12,
num_queries=1) — num_queries=1 is mandatory (the default burns LLM quota on
query generation).
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Optional

from . import providers, rerank, stores

logger = logging.getLogger("alpha.retrieval")

# --- Guardrail constants (names/locations frozen; QA tunes the VALUES) --------
RERANK_SCORE_FLOOR = 0.30  # gemini + rerank on: top rerank score below this => no_answer
FUSED_OVERLAP_FLOOR = 1    # gemini + rerank off: min distinct non-stopword question
                           # terms that must appear in the top-3 snippets
NONE_MODE_OVERLAP_FLOOR = 1  # none mode: same overlap requirement (with bm25-zero check)

# Reporting-verb boilerplate: request verbs a question uses to ASK for a figure
# ("did X report/post/provide ..."). Excluded from the exclusive-topic check so
# they can never masquerade as the question's subject.
REPORTING_VERBS = frozenset(
    """announce announced deliver delivered disclose disclosed do did done get got
    give gave have had hold held make made operate operated post posted provide
    provided report reported say said show showed state stated tell told work
    worked""".split()
)

# Period tokens (quarters, fiscal years, halves, years) get strict handling in
# none mode: a question that names a period the retrieved text never mentions
# (e.g. Q3 when only Q2 exists) is unanswerable from these documents.
_PERIOD_RE = re.compile(r"^(?:q[1-4]|fy(?:19|20)\d{2}|h[12]|(?:19|20)\d{2})$")
_QUARTER_ORDINALS = {"q1": "first", "q2": "second", "q3": "third", "q4": "fourth"}

DENSE_TOP_K = 8
SPARSE_TOP_K = 8
FUSION_POOL = 12

STOPWORDS = frozenset(
    """a an and are as at be been but by can could did do does for from had has have
    how i if in into is it its of on or s t that the their there these they this to
    was were what when where which who why will with would you your""".split()
)

_WORD_RE = re.compile(r"[a-z0-9$%.]+")


@dataclass
class RetrievalResult:
    nodes: list = field(default_factory=list)
    no_answer: bool = False
    retrieval_ms: int = 0
    rerank_ms: int = 0


# --- helpers ------------------------------------------------------------------

def strip_provenance(node) -> str:
    """Chunk text without the `[doc — p.N]` prefix (snippets, overlap checks)."""
    md = node.metadata or {}
    text = node.get_content()
    for prefix in (
        f"[{md.get('doc_name')} — p.{md.get('page')}] ",
        f"[{md.get('doc_name')}] ",
    ):
        if text.startswith(prefix):
            return text[len(prefix):]
    return text


def _terms(text: str) -> set[str]:
    return {t.strip(".$%") for t in _WORD_RE.findall((text or "").lower())} - STOPWORDS - {""}


def term_overlap(question: str, nodes: list, top_n: int = 3) -> int:
    """Distinct non-stopword question terms appearing in the top-N snippets
    (provenance prefixes stripped so filenames can't fake relevance)."""
    q_terms = _terms(question)
    if not q_terms:
        return 0
    snippet_terms: set[str] = set()
    for n in nodes[:top_n]:
        snippet_terms |= _terms(strip_provenance(n.node))
    return len(q_terms & snippet_terms)


def _words(text: str) -> set[str]:
    """Word-boundary token set (no substring matching — 'work' != 'workflow')."""
    return {t.strip(".$%") for t in _WORD_RE.findall((text or "").lower())} - {""}


def _entity_candidates(question: str) -> set[str]:
    """Capitalized, non-sentence-initial tokens — the entities the question
    names (company names, tickers, acronyms). Possessives stripped."""
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9&.'’-]*", question)
    out: set[str] = set()
    for i, tok in enumerate(tokens):
        if i == 0 or not tok[0].isupper():
            continue
        w = tok.lower()
        for suffix in ("'s", "’s"):
            w = w.removesuffix(suffix)
        w = w.strip(".&'-’")
        if len(w) >= 2 and w not in STOPWORDS and not _PERIOD_RE.match(w):
            out.add(w)
    return out


def _period_satisfied(period: str, words: set[str]) -> bool:
    if period in words:
        return True
    if period in _QUARTER_ORDINALS:  # q2 <-> "second quarter"
        return _QUARTER_ORDINALS[period] in words and "quarter" in words
    if period.startswith("fy"):  # fy2026 <-> 2026
        return period[2:] in words
    if period.isdigit():  # 2026 <-> fy2026
        return ("fy" + period) in words
    return False


def structural_no_answer(question: str, kept: list, corpus_nodes: list) -> bool:
    """none-mode structural guardrail (pre-LLM, zero API cost). Refuses when:

    1. the question names an entity that appears nowhere in the top-3 texts
       (unknown company, or a doc_ids scope that excludes it);
    2. the question names a period (Q3, FY2027, ...) that the top-3 texts never
       mention, even via 'q2' <-> 'second quarter' / 'fy2026' <-> '2026';
    3. cross-document trap: some topic term exists in the (scoped) corpus ONLY
       in documents where none of the named entities appear — the only evidence
       for the topic belongs to a different company's document (spec §7).
    """
    top_words: set[str] = set()
    for n in kept[:3]:
        top_words |= _words(strip_provenance(n.node))

    entities = _entity_candidates(question)
    if entities and not (entities & top_words):
        return True

    q_words = _words(question)
    periods = {w for w in q_words if _PERIOD_RE.match(w)}
    for p in periods:
        if not _period_satisfied(p, top_words):
            return True

    if entities:
        doc_words: dict = {}
        for node in corpus_nodes:
            d = (node.metadata or {}).get("doc_id")
            doc_words.setdefault(d, set()).update(_words(strip_provenance(node)))
        entity_docs = {d for d, ws in doc_words.items() if ws & entities}
        content = {
            w
            for w in q_words - STOPWORDS - REPORTING_VERBS - entities - periods
            if len(w) >= 2
        }
        for term in content:
            homes = {d for d, ws in doc_words.items() if term in ws}
            if homes and entity_docs and not (homes & entity_docs):
                return True
    return False


# --- BM25 ---------------------------------------------------------------------

_bm25_cache: dict = {"epoch": None, "retriever": None}


def _build_bm25(nodes: list, top_k: int):
    from llama_index.retrievers.bm25 import BM25Retriever

    return BM25Retriever.from_defaults(nodes=nodes, similarity_top_k=max(1, min(top_k, len(nodes))))


def get_bm25(doc_ids: Optional[list[str]] = None):
    """Unfiltered retriever cached per stores.epoch; scoped requests rebuild
    over the filtered subset (corpora are small)."""
    store = stores.get_store()
    if doc_ids:
        return _build_bm25(store.nodes_for(doc_ids), FUSION_POOL)
    if _bm25_cache["retriever"] is None or _bm25_cache["epoch"] != store.epoch:
        nodes = store.nodes_for(None)
        _bm25_cache["retriever"] = _build_bm25(nodes, FUSION_POOL) if nodes else None
        _bm25_cache["epoch"] = store.epoch
    return _bm25_cache["retriever"]


def _bm25_retrieve(retriever, question: str, k: int, corpus_size: int) -> list:
    retriever.similarity_top_k = max(1, min(k, corpus_size))
    return retriever.retrieve(question)


# --- main entry ---------------------------------------------------------------

def run_retrieval(question: str, doc_ids: Optional[list[str]], top_k: int) -> RetrievalResult:
    store = stores.get_store()
    bundle = providers.get_bundle()
    rerank_on = rerank.effective_rerank() == "on"

    t0 = time.perf_counter()
    corpus = store.nodes_for(doc_ids if doc_ids else None)
    if not corpus:  # empty corpus (or empty scope) => no_answer, zero LLM spend
        return RetrievalResult(no_answer=True, retrieval_ms=_ms_since(t0))

    if bundle.provider == "none":
        return _run_none_mode(question, doc_ids, top_k, corpus, rerank_on, t0)
    return _run_gemini_mode(question, doc_ids, top_k, corpus, rerank_on, t0, bundle)


def _run_none_mode(question, doc_ids, top_k, corpus, rerank_on, t0) -> RetrievalResult:
    pool = FUSION_POOL if rerank_on else top_k
    retriever = get_bm25(doc_ids if doc_ids else None)
    results = _bm25_retrieve(retriever, question, pool, len(corpus))
    bm25_top = max((r.score or 0.0) for r in results) if results else 0.0
    retrieval_ms = _ms_since(t0)

    rerank_ms = 0
    if rerank_on:
        t1 = time.perf_counter()
        kept = rerank.rerank_nodes(question, results, top_k)
        rerank_ms = _ms_since(t1)
    else:
        kept = results[:top_k]

    no_answer = (
        not kept
        or bm25_top <= 0.0
        or term_overlap(question, kept) < NONE_MODE_OVERLAP_FLOOR
        or structural_no_answer(question, kept, corpus)
    )
    return RetrievalResult(kept, no_answer, retrieval_ms, rerank_ms)


def _run_gemini_mode(question, doc_ids, top_k, corpus, rerank_on, t0, bundle) -> RetrievalResult:
    from llama_index.core import VectorStoreIndex
    from llama_index.core.retrievers import QueryFusionRetriever
    from llama_index.core.schema import QueryBundle
    from llama_index.core.vector_stores.types import (
        FilterOperator,
        MetadataFilter,
        MetadataFilters,
    )
    from llama_index.vector_stores.chroma import ChromaVectorStore

    # Query embedding goes through the same sha256 cache as chunk embeddings.
    query_vec = providers.embed_texts_cached([question], bundle.embed_model_name)[0]

    vector_store = ChromaVectorStore(chroma_collection=stores.get_store().collection)
    index = VectorStoreIndex.from_vector_store(vector_store, embed_model=bundle.embed_model)
    filters = None
    if doc_ids:
        filters = MetadataFilters(
            filters=[MetadataFilter(key="doc_id", value=list(doc_ids), operator=FilterOperator.IN)]
        )
    dense = index.as_retriever(similarity_top_k=DENSE_TOP_K, filters=filters)

    sparse = get_bm25(doc_ids if doc_ids else None)
    sparse.similarity_top_k = max(1, min(SPARSE_TOP_K, len(corpus)))

    fusion = QueryFusionRetriever(
        [dense, sparse],
        mode="reciprocal_rerank",
        similarity_top_k=FUSION_POOL,
        num_queries=1,  # mandatory — no LLM query expansion
        use_async=False,
    )
    fused = fusion.retrieve(QueryBundle(query_str=question, embedding=query_vec))
    retrieval_ms = _ms_since(t0)

    rerank_ms = 0
    if rerank_on:
        t1 = time.perf_counter()
        kept = rerank.rerank_nodes(question, fused, top_k)
        rerank_ms = _ms_since(t1)
        top_score = (kept[0].score or 0.0) if kept else 0.0
        no_answer = (not kept) or top_score < RERANK_SCORE_FLOOR
    else:
        kept = fused[:top_k]
        no_answer = (not kept) or term_overlap(question, kept) < FUSED_OVERLAP_FLOOR

    return RetrievalResult(kept, no_answer, retrieval_ms, rerank_ms)


def _ms_since(t_start: float) -> int:
    return int(round((time.perf_counter() - t_start) * 1000))
