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

v1.2 adds the retrieval inspector (§1.9). It is an OBSERVABILITY VIEW over work
this module already did: `_RecordingRetriever` wraps the dense and sparse
retrievers on EVERY query — explain or not — and stores the list each one
returned, verbatim. Only `build_pipeline` serialization is conditional on
`explain`, so the executed code path is identical in both modes and explain can
never move a ranking. Zero extra LLM calls, zero extra embedding calls, no
second retrieval pass, nothing re-scored.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Optional

from llama_index.core.retrievers import BaseRetriever

from . import providers, rerank, stores
from .providers import get_bundle  # bound here so the bundle is a seam per module

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

# Inspector display caps (§1.9.3). DISPLAY ONLY — they bound what the debugging
# view shows and never touch retrieval depth, ranking or citations.
EXPLAIN_BM25_K = 8
EXPLAIN_DENSE_K = 8
EXPLAIN_POOL_K = 12
EXPLAIN_RERANK_K = 6
EXPLAIN_SNIPPET_MAX_CHARS = 120

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
    pipeline: Optional[dict] = None  # §1.9 — populated only when explain=True


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


def _record(checks: Optional[dict], name: str, passed: bool) -> None:
    """Write one guardrail outcome. Write-only bookkeeping: it records a
    comparison the guardrail already made and can never change a decision."""
    if checks is not None:
        checks[name] = "pass" if passed else "fail"


def structural_no_answer(
    question: str, kept: list, corpus_nodes: list, checks: Optional[dict] = None
) -> bool:
    """none-mode structural guardrail (pre-LLM, zero API cost). Refuses when:

    1. the question names an entity that appears nowhere in the top-3 texts
       (unknown company, or a doc_ids scope that excludes it);
    2. the question names a period (Q3, FY2027, ...) that the top-3 texts never
       mention, even via 'q2' <-> 'second quarter' / 'fy2026' <-> '2026';
    3. cross-document trap: some topic term exists in the (scoped) corpus ONLY
       in documents where none of the named entities appear — the only evidence
       for the topic belongs to a different company's document (spec §7).

    `checks` (§1.9.4) is optional write-only bookkeeping: each check that is
    actually evaluated records "pass"/"fail" under its frozen name. The return
    value, the short-circuit order and the pre-LLM placement are unchanged —
    checks after the first failure are never reached and so are never reported.
    """
    top_words: set[str] = set()
    for n in kept[:3]:
        top_words |= _words(strip_provenance(n.node))

    entities = _entity_candidates(question)
    if entities and not (entities & top_words):
        _record(checks, "entity_presence", False)
        return True
    _record(checks, "entity_presence", True)

    q_words = _words(question)
    periods = {w for w in q_words if _PERIOD_RE.match(w)}
    for p in periods:
        if not _period_satisfied(p, top_words):
            _record(checks, "period_presence", False)
            return True
    _record(checks, "period_presence", True)

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
                _record(checks, "exclusive_topic", False)
                return True
    _record(checks, "exclusive_topic", True)
    return False


# --- inspector capture (§1.9) -------------------------------------------------

class _RecordingRetriever(BaseRetriever):
    """Passthrough wrapper that stores what the wrapped retriever returned.

    Installed around the dense and sparse retrievers on EVERY query (explain or
    not) so the executed path never differs between modes. It delegates to the
    inner retriever's own `retrieve()` — the identical call `QueryFusionRetriever`
    would otherwise have made — and records the returned list VERBATIM: same
    objects, same order, same scores. It never sorts, filters, truncates,
    copies-with-changes, or re-invokes anything, and it issues no call of its
    own: no LLM, no embedding, no second pass.
    """

    def __init__(self, inner, sink: dict, label: str) -> None:
        self._inner = inner
        self._sink = sink
        self._label = label
        super().__init__()

    def retrieve(self, str_or_query_bundle):
        results = self._inner.retrieve(str_or_query_bundle)
        self._record(results)
        return results

    async def aretrieve(self, str_or_query_bundle):
        results = await self._inner.aretrieve(str_or_query_bundle)
        self._record(results)
        return results

    def _record(self, results) -> None:
        """Store the list verbatim plus a snapshot of the scores AS SEEN HERE.

        `rerank.rerank_nodes` replaces `.score` on the very NodeWithScore
        objects a retriever returned, so the snapshot is what keeps each stage
        reporting the score IT produced (§1.9.3) instead of the cross-encoder's.
        Reading twelve floats is not a second pass: nothing is re-invoked,
        re-scored, sorted or filtered.
        """
        self._sink[self._label] = results
        self._sink[self._label + ":scores"] = [n.score for n in results]

    def _retrieve(self, query_bundle):  # pragma: no cover - retrieve() intercepts
        return self._inner.retrieve(query_bundle)


def _head_snippet(text: str, limit: int = EXPLAIN_SNIPPET_MAX_CHARS) -> str:
    """Chunk HEAD, whitespace collapsed, word-boundary cut with a trailing '…'.

    Deliberately NOT synthesis.make_snippet: the inspector must never couple
    itself to the citation-window algorithm (§1.9.3), and retrieval.py has no
    import edge to synthesis.
    """
    collapsed = " ".join((text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    piece = collapsed[: limit - 1]
    cut = piece.rfind(" ")
    if cut > 0:
        piece = piece[:cut]
    return piece.rstrip() + "…"


def _rank_map(nodes: Optional[list]) -> dict:
    """node_id -> 1-based rank within that retriever's own result list."""
    return {n.node.node_id: i for i, n in enumerate(nodes or [], start=1)}


def _round4(score) -> float:
    return round(float(score or 0.0), 4)


def _stage_item(nws, score=None) -> dict:
    md = nws.node.metadata or {}
    page = md.get("page")
    return {
        "doc_id": md.get("doc_id"),
        "doc_name": md.get("doc_name"),
        "page": page if isinstance(page, int) else None,
        "chunk_ix": int(md.get("chunk_ix") or 0),
        "score": _round4(nws.score if score is None else score),
        "snippet": _head_snippet(strip_provenance(nws.node)),
    }


def _scores_at(snapshot: Optional[list], index: int):
    """The score a stage produced for its i-th item (None => use the node's)."""
    if snapshot is None or index >= len(snapshot):
        return None
    return snapshot[index]


def build_pipeline(
    mode: str,
    rerank_state: str,
    top_k: int,
    bm25: Optional[list] = None,
    bm25_k: Optional[int] = None,
    bm25_scores: Optional[list] = None,
    dense: Optional[list] = None,
    dense_scores: Optional[list] = None,
    fused: Optional[list] = None,
    fused_scores: Optional[list] = None,
    fusion_method: str = "rrf",
    fusion_k: Optional[int] = None,
    kept: Optional[list] = None,
    rerank_model: Optional[str] = None,
    checks: Optional[dict] = None,
    passed: bool = True,
) -> dict:
    """Serialize already-captured stage data into the §1.9 shape.

    PURE serialization — rounding, snippet truncation and rank lookups only.
    It makes no calls into providers, rerank or stores, and it never retrieves.
    A stage exists iff work happened: `dense` is omitted entirely in keyless
    mode and `rerank` is omitted entirely when rerank is effectively off, while
    `fusion` is ALWAYS present (`method:"passthrough"` in keyless mode) so
    `before_rank` always has an anchor (resolution 7).
    """
    stages: list[dict] = []

    if bm25 is not None:
        stages.append(
            {
                "stage": "bm25",
                "k": int(bm25_k if bm25_k is not None else SPARSE_TOP_K),
                "items": [
                    _stage_item(n, _scores_at(bm25_scores, i))
                    for i, n in enumerate(bm25[:EXPLAIN_BM25_K])
                ],
            }
        )

    if dense is not None:
        stages.append(
            {
                "stage": "dense",
                "k": DENSE_TOP_K,
                "items": [
                    _stage_item(n, _scores_at(dense_scores, i))
                    for i, n in enumerate(dense[:EXPLAIN_DENSE_K])
                ],
            }
        )

    if fused is not None:
        bm25_ranks = _rank_map(bm25)
        dense_ranks = _rank_map(dense)
        items = []
        for i, nws in enumerate(fused[:EXPLAIN_POOL_K]):
            item = _stage_item(nws, _scores_at(fused_scores, i))
            item["bm25_rank"] = bm25_ranks.get(nws.node.node_id)
            item["dense_rank"] = dense_ranks.get(nws.node.node_id)
            items.append(item)
        stages.append(
            {
                "stage": "fusion",
                "method": fusion_method,
                # `k` is the depth this stage ACTUALLY operated at, never a
                # constant echoed back: keyless rerank-off passthrough reports
                # top_k, not FUSION_POOL (§1.9.3). An inspector that reports a
                # configured value it did not use defeats its own purpose.
                "k": int(fusion_k if fusion_k is not None else FUSION_POOL),
                "items": items,
            }
        )

    if kept is not None:
        pool_ranks = _rank_map(fused)  # before_rank spans the WHOLE pool
        items = []
        for after_rank, nws in enumerate(kept[:EXPLAIN_RERANK_K], start=1):
            item = _stage_item(nws)
            item["before_rank"] = pool_ranks.get(nws.node.node_id)
            item["after_rank"] = after_rank
            items.append(item)
        stages.append(
            {
                "stage": "rerank",
                "model": rerank_model,
                "k": int(top_k),
                "items": items,
            }
        )

    stages.append(
        {"stage": "guardrail", "passed": bool(passed), "checks": dict(checks or {})}
    )
    return {
        "mode": mode,
        "rerank": rerank_state,
        "top_k": int(top_k),
        "stages": stages,
    }


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


def _bm25_retrieve(retriever, question: str, k: int, corpus_size: int, through=None) -> list:
    """Depth is set on the real retriever; the call may go through a recorder
    (identical work, one list assignment more)."""
    retriever.similarity_top_k = max(1, min(k, corpus_size))
    return (through or retriever).retrieve(question)


# --- main entry ---------------------------------------------------------------

def run_retrieval(
    question: str,
    doc_ids: Optional[list[str]],
    top_k: int,
    explain: bool = False,
) -> RetrievalResult:
    """The four §5 paths, unchanged. `explain` only decides whether the stage
    data captured on every query is serialized into `pipeline` (§1.9.1)."""
    store = stores.get_store()
    bundle = get_bundle()
    rerank_on = rerank.effective_rerank() == "on"
    mode = bundle.provider
    rerank_state = "on" if rerank_on else "off"

    t0 = time.perf_counter()
    corpus = store.nodes_for(doc_ids if doc_ids else None)
    if not corpus:  # empty corpus (or empty scope) => no_answer, zero LLM spend
        pipeline = None
        if explain:
            # Short-circuits before retrieval runs: the only stage that happened
            # is the guardrail, and the only check it reached is `nonempty`.
            pipeline = build_pipeline(
                mode, rerank_state, top_k, checks={"nonempty": "fail"}, passed=False
            )
        return RetrievalResult(no_answer=True, retrieval_ms=_ms_since(t0), pipeline=pipeline)

    if mode == "none":
        return _run_none_mode(
            question, doc_ids, top_k, corpus, rerank_on, t0, explain, rerank_state
        )
    return _run_gemini_mode(
        question, doc_ids, top_k, corpus, rerank_on, t0, bundle, explain, rerank_state
    )


def _run_none_mode(
    question, doc_ids, top_k, corpus, rerank_on, t0, explain, rerank_state
) -> RetrievalResult:
    pool = FUSION_POOL if rerank_on else top_k
    retriever = get_bm25(doc_ids if doc_ids else None)
    capture: dict = {}
    # Recorder installed unconditionally — explain never changes the path.
    recorder = _RecordingRetriever(retriever, capture, "bm25")
    results = _bm25_retrieve(retriever, question, pool, len(corpus), recorder)
    bm25_scores = capture.get("bm25:scores", [r.score for r in results])
    bm25_top = max((r.score or 0.0) for r in results) if results else 0.0
    retrieval_ms = _ms_since(t0)

    rerank_ms = 0
    if rerank_on:
        t1 = time.perf_counter()
        kept = rerank.rerank_nodes(question, results, top_k)
        rerank_ms = _ms_since(t1)
    else:
        kept = results[:top_k]

    checks: dict = {}
    no_answer = _none_mode_guardrail(question, kept, corpus, bm25_top, checks)

    pipeline = None
    if explain:
        pipeline = build_pipeline(
            "none",
            rerank_state,
            top_k,
            bm25=capture.get("bm25", results),
            bm25_k=pool,  # the sparse depth this path actually asked for
            bm25_scores=bm25_scores,
            dense=None,  # keyless: dense never ran, so the stage is omitted
            fused=capture.get("bm25", results),  # passthrough anchors before_rank
            fused_scores=bm25_scores,
            fusion_method="passthrough",
            fusion_k=pool,  # no fusion ran: the candidate pool is this deep
            kept=kept if rerank_on else None,
            rerank_model=rerank.effective_model_name(),
            checks=checks,
            passed=not no_answer,
        )
    return RetrievalResult(kept, no_answer, retrieval_ms, rerank_ms, pipeline)


def _none_mode_guardrail(question, kept, corpus, bm25_top, checks: dict) -> bool:
    """The v1.1 boolean, decomposed so each evaluated check can record itself.

    Identical semantics and identical short-circuit order to the original
    `not kept or bm25_top <= 0 or overlap < floor or structural(...)` chain:
    checks after the first failure are never evaluated and never reported.
    """
    if not kept:
        _record(checks, "nonempty", False)
        return True
    _record(checks, "nonempty", True)
    if bm25_top <= 0.0:
        _record(checks, "bm25_nonzero", False)
        return True
    _record(checks, "bm25_nonzero", True)
    if term_overlap(question, kept) < NONE_MODE_OVERLAP_FLOOR:
        _record(checks, "term_overlap", False)
        return True
    _record(checks, "term_overlap", True)
    return structural_no_answer(question, kept, corpus, checks)


def _run_gemini_mode(
    question, doc_ids, top_k, corpus, rerank_on, t0, bundle, explain, rerank_state
) -> RetrievalResult:
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
    # It is computed EXACTLY ONCE per query and handed to fusion inside the
    # QueryBundle below; explain never re-embeds anything.
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

    # Recorders wrap both retrievers on EVERY query (explain or not).
    capture: dict = {}
    fusion = QueryFusionRetriever(
        [
            _RecordingRetriever(dense, capture, "dense"),
            _RecordingRetriever(sparse, capture, "bm25"),
        ],
        mode="reciprocal_rerank",
        similarity_top_k=FUSION_POOL,
        num_queries=1,  # mandatory — no LLM query expansion
        use_async=False,
    )
    fused = fusion.retrieve(QueryBundle(query_str=question, embedding=query_vec))
    # Snapshot the RRF scores before the reranker replaces them in place.
    fused_scores = [n.score for n in fused]
    retrieval_ms = _ms_since(t0)

    checks: dict = {}
    rerank_ms = 0
    if rerank_on:
        t1 = time.perf_counter()
        kept = rerank.rerank_nodes(question, fused, top_k)
        rerank_ms = _ms_since(t1)
        no_answer = _gemini_guardrail_rerank_on(kept, checks)
    else:
        kept = fused[:top_k]
        no_answer = _gemini_guardrail_rerank_off(question, kept, checks)

    pipeline = None
    if explain:
        pipeline = build_pipeline(
            "gemini",
            rerank_state,
            top_k,
            bm25=capture.get("bm25", []),
            bm25_k=SPARSE_TOP_K,
            bm25_scores=capture.get("bm25:scores"),
            dense=capture.get("dense", []),
            dense_scores=capture.get("dense:scores"),
            fused=fused,
            fused_scores=fused_scores,
            fusion_method="rrf",
            fusion_k=FUSION_POOL,  # QueryFusionRetriever(similarity_top_k=12)
            kept=kept if rerank_on else None,
            rerank_model=rerank.effective_model_name(),
            checks=checks,
            passed=not no_answer,
        )
    return RetrievalResult(kept, no_answer, retrieval_ms, rerank_ms, pipeline)


def _gemini_guardrail_rerank_on(kept, checks: dict) -> bool:
    if not kept:
        _record(checks, "nonempty", False)
        return True
    _record(checks, "nonempty", True)
    if (kept[0].score or 0.0) < RERANK_SCORE_FLOOR:
        _record(checks, "rerank_floor", False)
        return True
    _record(checks, "rerank_floor", True)
    return False


def _gemini_guardrail_rerank_off(question, kept, checks: dict) -> bool:
    if not kept:
        _record(checks, "nonempty", False)
        return True
    _record(checks, "nonempty", True)
    if term_overlap(question, kept) < FUSED_OVERLAP_FLOOR:
        _record(checks, "term_overlap", False)
        return True
    _record(checks, "term_overlap", True)
    return False


def _ms_since(t_start: float) -> int:
    return int(round((time.perf_counter() - t_start) * 1000))
