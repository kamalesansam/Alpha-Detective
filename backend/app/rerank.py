"""Local cross-encoder rerank stage — free, no API, optional at runtime.

flashrank (tiny ONNX models) is preferred; a sentence-transformers
cross-encoder is attempted only if that library happens to be installed.
The first-run model download happens at startup (init_reranker), never
mid-query; on ANY failure we log one line and run with rerank effectively
off while RERANK=on stays in config — /api/health reports the effective state.

Test seam: `set_scorer(fn)` injects any `fn(query, passages) -> list[float]`.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from . import config

logger = logging.getLogger("alpha.rerank")

# fn(query, passages) -> one relevance score per passage (higher = better).
Scorer = Callable[[str, list[str]], list[float]]

_scorer: Optional[Scorer] = None
_effective_on: bool = False


def set_scorer(scorer: Optional[Scorer]) -> None:
    """Inject a scorer (tests) or clear it. None => rerank effectively off."""
    global _scorer, _effective_on
    _scorer = scorer
    _effective_on = scorer is not None


def _build_flashrank_scorer() -> Scorer:
    from flashrank import Ranker, RerankRequest  # lazy import

    config.RERANK_MODEL_DIR.mkdir(parents=True, exist_ok=True)
    ranker = Ranker(cache_dir=str(config.RERANK_MODEL_DIR))  # default tiny ONNX model

    def score(query: str, passages: list[str]) -> list[float]:
        req = RerankRequest(
            query=query,
            passages=[{"id": i, "text": p} for i, p in enumerate(passages)],
        )
        results = ranker.rerank(req)
        scores = [0.0] * len(passages)
        for r in results:
            scores[int(r["id"])] = float(r["score"])
        return scores

    # Warm the model once at startup so nothing downloads/compiles mid-query.
    score("warmup", ["warmup passage"])
    return score


def _build_sentence_transformers_scorer() -> Scorer:
    from sentence_transformers import CrossEncoder  # only if user installed it

    model = CrossEncoder("cross-encoder/ms-marco-MiniLM-L6-v2")

    def score(query: str, passages: list[str]) -> list[float]:
        return [float(s) for s in model.predict([(query, p) for p in passages])]

    score("warmup", ["warmup passage"])
    return score


def init_reranker() -> bool:
    """Attempt reranker init at startup. Never raises."""
    global _scorer, _effective_on
    settings = config.get_settings()
    if settings.rerank != "on":
        _scorer, _effective_on = None, False
        logger.info("rerank requested off (RERANK=off)")
        return False
    errors: list[str] = []
    for builder in (_build_flashrank_scorer, _build_sentence_transformers_scorer):
        name = builder.__name__.replace("_build_", "").replace("_scorer", "")
        try:
            _scorer = builder()
            _effective_on = True
            logger.info("reranker ready (%s)", name)
            return True
        except Exception as exc:  # noqa: BLE001 — reranker is best-effort local
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
    _scorer, _effective_on = None, False
    logger.warning(
        "reranker unavailable — running with rerank effectively off (%s)",
        " | ".join(errors),
    )
    return False


def effective_rerank() -> str:
    """The truth /api/health reports."""
    return "on" if _effective_on else "off"


def rerank_nodes(question: str, nodes: list, keep: int) -> list:
    """Score the fused pool with the local cross-encoder; return top `keep`
    (each node's .score replaced by its rerank score)."""
    if not _effective_on or _scorer is None or not nodes:
        return nodes[:keep]
    passages = [n.node.get_content() for n in nodes]
    scores = _scorer(question, passages)
    for n, s in zip(nodes, scores):
        n.score = float(s)
    ranked = sorted(nodes, key=lambda n: n.score if n.score is not None else 0.0, reverse=True)
    return ranked[:keep]
