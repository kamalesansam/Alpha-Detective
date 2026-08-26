"""Provider layer — the ONLY module that talks to Google Gemini.

Import law: only this module may import `google.genai`,
`llama_index.llms.google_genai`, or `llama_index.embeddings.google_genai`
(the deprecated `llama-index-*-gemini` packages are banned entirely).

LLM / embedding objects are constructed lazily behind small factory functions
(`_make_llm`, `_make_embed_model`) so tests can monkeypatch them; in `none`
mode no Google object is ever constructed.

`llama_index.core.Settings.llm` / `.embed_model` are ALWAYS set explicitly at
startup — in `none` mode explicitly to None (LlamaIndex resolves that to its
inert Mock objects) — so the silent OpenAI default can never trigger.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from dataclasses import dataclass
from typing import Any, Optional

from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from . import config

logger = logging.getLogger("alpha.providers")

# Fallback chains for `auto` model resolution (CLAUDE_CODE_PROMPT.md §6.1).
LLM_MODEL_CHAIN = ["gemini-flash-latest", "gemini-2.5-flash", "gemini-2.0-flash"]
EMBED_MODEL_CHAIN = ["gemini-embedding-001", "gemini-embedding-2-preview"]

DEFAULT_RETRY_AFTER_S = 30
MAX_ATTEMPTS = 4  # tenacity: exponential backoff + jitter, <=4 attempts


class RateLimitedError(Exception):
    """Gemini 429/503 after backoff exhausted. Mapped to HTTP 429."""

    def __init__(self, message: str = "Free-tier rate limit hit", retry_after_s: int = DEFAULT_RETRY_AFTER_S):
        super().__init__(message)
        self.retry_after_s = int(retry_after_s)


class ProviderError(Exception):
    """Any other Gemini API failure (auth, non-retryable 4xx/5xx, network). Mapped to HTTP 502."""


@dataclass
class ProviderBundle:
    provider: str  # "gemini" | "none"
    llm: Any = None
    embed_model: Any = None
    llm_model_name: Optional[str] = None
    embed_model_name: Optional[str] = None


_bundle: Optional[ProviderBundle] = None
_embed_cache: Optional[dict] = None
_embed_cache_lock = threading.Lock()

# Daily LLM budget (§1.11). The counter lives on disk so a restart cannot
# hand out a fresh allowance; the lock makes check-and-increment atomic
# within the single instance the free tier runs.
_budget_lock = threading.RLock()
_budget_unwritable_logged = False


# --- retryable-error detection ------------------------------------------------

def _status_code_of(exc: Exception) -> Optional[int]:
    for attr in ("code", "status_code", "status"):
        v = getattr(exc, attr, None)
        if isinstance(v, int):
            return v
        if isinstance(v, str) and v.isdigit():
            return int(v)
    m = re.search(r"\b(429|503)\b", str(exc))
    return int(m.group(1)) if m else None


def _is_retryable(exc: BaseException) -> bool:
    if not isinstance(exc, Exception):
        return False
    code = _status_code_of(exc)
    if code in (429, 503):
        return True
    text = str(exc).upper()
    return "RESOURCE_EXHAUSTED" in text or "UNAVAILABLE" in text


def _parse_retry_after_s(exc: Exception) -> int:
    """Best-effort parse of the provider-suggested retry delay, else 30."""
    text = str(exc)
    m = re.search(r"retry(?:_delay|Delay)?[^0-9]{0,20}?(\d+(?:\.\d+)?)\s*s", text, re.IGNORECASE)
    if m:
        try:
            return max(1, int(float(m.group(1))))
        except ValueError:
            pass
    return DEFAULT_RETRY_AFTER_S


_with_backoff = retry(
    retry=retry_if_exception(_is_retryable),
    wait=wait_exponential_jitter(initial=1, max=16),
    stop=stop_after_attempt(MAX_ATTEMPTS),
    reraise=True,
)


# --- lazy factories (mockable seams) -----------------------------------------

def _make_genai_client(api_key: str):
    from google import genai  # lazy: only in gemini mode

    return genai.Client(api_key=api_key)


def _make_llm(api_key: str, model: str):
    from llama_index.llms.google_genai import GoogleGenAI  # lazy

    return GoogleGenAI(model=model, api_key=api_key, temperature=0.1, max_tokens=1024)


def _make_embed_model(api_key: str, model: str):
    from llama_index.embeddings.google_genai import GoogleGenAIEmbedding  # lazy

    return GoogleGenAIEmbedding(model_name=model, api_key=api_key, embed_batch_size=100)


# --- model resolution ---------------------------------------------------------

def resolve_models(settings: config.Settings) -> tuple[Optional[str], Optional[str]]:
    """List live models via the API; first match in each fallback chain.

    Explicit (non-`auto`) ids are trusted verbatim. `(None, None)` in none mode.
    Logs resolved names once; never the key.
    """
    if settings.effective_provider != "gemini":
        return None, None

    llm_name = settings.gemini_llm_model
    embed_name = settings.gemini_embed_model
    if llm_name != "auto" and embed_name != "auto":
        return llm_name, embed_name

    try:
        client = _make_genai_client(settings.google_api_key.strip())
        live = [
            (getattr(m, "name", "") or "").removeprefix("models/")
            for m in client.models.list()
        ]
    except Exception as exc:  # noqa: BLE001 - normalized to a typed error
        if _is_retryable(exc):
            raise RateLimitedError(retry_after_s=_parse_retry_after_s(exc)) from exc
        raise ProviderError(
            "could not list Gemini models to resolve `auto` (check the API key and network)"
        ) from exc

    live_set = set(live)
    if llm_name == "auto":
        llm_name = next((c for c in LLM_MODEL_CHAIN if c in live_set), None)
    if embed_name == "auto":
        embed_name = next((c for c in EMBED_MODEL_CHAIN if c in live_set), None)
    if not llm_name or not embed_name:
        raise ProviderError(
            "no Gemini model from the fallback chains is available on this account "
            f"(llm chain: {LLM_MODEL_CHAIN}, embed chain: {EMBED_MODEL_CHAIN})"
        )
    return llm_name, embed_name


def init_none_mode() -> ProviderBundle:
    """Enter retrieval-only mode: no LLM, no embeddings, no Google objects.

    Used both for an explicit `PROVIDER=none` and as the `auto` fallback when
    provider startup fails (ratified r3).
    """
    global _bundle
    from llama_index.core import Settings as LISettings

    LISettings.llm = None
    LISettings.embed_model = None
    _bundle = ProviderBundle(provider="none")
    logger.info("provider=none (retrieval-only mode): no LLM, no embeddings")
    return _bundle


def init_providers(settings: config.Settings) -> ProviderBundle:
    """Build the provider bundle and set llama_index Settings explicitly.

    none mode: Settings.llm / Settings.embed_model are explicitly set to None
    (LlamaIndex resolves to Mock objects — never the OpenAI default) and no
    Google object is constructed.
    """
    global _bundle

    provider = settings.effective_provider
    if provider == "none":
        return init_none_mode()

    from llama_index.core import Settings as LISettings

    llm_name, embed_name = resolve_models(settings)
    key = settings.google_api_key.strip()
    llm = _make_llm(key, llm_name)
    embed_model = _make_embed_model(key, embed_name)
    LISettings.llm = llm
    LISettings.embed_model = embed_model
    _bundle = ProviderBundle(
        provider="gemini",
        llm=llm,
        embed_model=embed_model,
        llm_model_name=llm_name,
        embed_model_name=embed_name,
    )
    logger.info("provider=gemini llm_model=%s embed_model=%s", llm_name, embed_name)
    return _bundle


def get_bundle() -> ProviderBundle:
    """Current bundle; lazily initialized (safe for tests that skip lifespan)."""
    global _bundle
    if _bundle is None:
        _bundle = init_providers(config.get_settings())
    return _bundle


# --- embedding cache ----------------------------------------------------------

def _cache_key(text: str, model_id: str) -> str:
    return hashlib.sha256((text + model_id).encode("utf-8")).hexdigest()


def _load_embed_cache() -> dict:
    global _embed_cache
    if _embed_cache is None:
        path = config.EMBED_CACHE_PATH
        cache: dict = {}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    cache = loaded
            except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                logger.warning("embed_cache.json unreadable — recreating empty (derived data)")
                cache = {}
        _embed_cache = cache
    return _embed_cache


def _persist_embed_cache() -> None:
    config.ensure_storage_dirs()
    tmp = config.EMBED_CACHE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(_load_embed_cache()), encoding="utf-8")
    os.replace(tmp, config.EMBED_CACHE_PATH)


def reset_embed_cache_state() -> None:
    """Drop the in-memory cache view (reload from disk on next use)."""
    global _embed_cache
    with _embed_cache_lock:
        _embed_cache = None


def embed_texts_cached(texts: list[str], model_id: str) -> list[list[float]]:
    """sha256(text+model_id)-cached embeddings; only misses hit the API (batched <=100).

    Used for chunk AND query embedding. Raises RateLimitedError after backoff
    exhaustion, ProviderError otherwise.
    """
    bundle = get_bundle()
    if bundle.provider != "gemini" or bundle.embed_model is None:
        raise ProviderError("embeddings unavailable in retrieval-only mode")

    with _embed_cache_lock:
        cache = _load_embed_cache()
        keys = [_cache_key(t, model_id) for t in texts]
        misses = [i for i, k in enumerate(keys) if k not in cache]
        vectors: list[Optional[list[float]]] = [cache.get(k) for k in keys]

    if misses:
        @_with_backoff
        def _embed_batch(batch: list[str]) -> list[list[float]]:
            return bundle.embed_model.get_text_embedding_batch(batch)

        fresh: list[list[float]] = []
        try:
            for start in range(0, len(misses), 100):
                idx_batch = misses[start : start + 100]
                fresh.extend(_embed_batch([texts[i] for i in idx_batch]))
        except Exception as exc:  # noqa: BLE001 - normalized to typed errors
            if _is_retryable(exc):
                raise RateLimitedError(retry_after_s=_parse_retry_after_s(exc)) from exc
            raise ProviderError("embedding request failed") from exc

        with _embed_cache_lock:
            cache = _load_embed_cache()
            for i, vec in zip(misses, fresh):
                vec = [float(x) for x in vec]
                cache[keys[i]] = vec
                vectors[i] = vec
            _persist_embed_cache()

    return [v for v in vectors]  # type: ignore[return-value]


# --- daily LLM budget (§1.11) -------------------------------------------------

def _utc_today() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _read_budget() -> dict:
    """`{"day","used"}` for TODAY. Derived data: anything unusable — missing,
    unparseable, or from a previous day — silently reads as today/0 (§3.4)."""
    today = _utc_today()
    path = config.LLM_BUDGET_PATH
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and raw.get("day") == today:
            used = raw.get("used")
            if isinstance(used, int) and not isinstance(used, bool) and used >= 0:
                return {"day": today, "used": used}
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        pass
    return {"day": today, "used": 0}


def _write_budget(state: dict) -> bool:
    """Atomic tmp + os.replace. False when the counter could not be persisted."""
    global _budget_unwritable_logged
    try:
        config.ensure_storage_dirs()
        tmp = config.LLM_BUDGET_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, config.LLM_BUDGET_PATH)
        return True
    except OSError:
        if not _budget_unwritable_logged:
            _budget_unwritable_logged = True
            logger.warning(
                "llm_budget.json is not writable — serving without budget "
                "accounting (availability over accounting)"
            )
        return False


def _budget_limit() -> int:
    return max(0, int(config.get_settings().daily_llm_budget))


def llm_budget_state() -> dict:
    """Read-only budget view for /api/health. Cheap, lazy day-rollover, never
    key material, never a mutation."""
    limit = _budget_limit()
    with _budget_lock:
        state = _read_budget()
    used = state["used"]
    return {
        "used": used,
        "limit": limit,
        "remaining": max(0, limit - used),
        "day": state["day"],
    }


def reserve_llm_call() -> bool:
    """Atomic check-and-increment; True = the caller may make its one LLM call.

    The ONLY mutator of the counter. Reservations are charged before the call
    and never refunded (a refund path invites double-spend under concurrency).
    Never raises for budget reasons: an unwritable counter logs once and
    returns True.
    """
    limit = _budget_limit()
    with _budget_lock:
        state = _read_budget()
        if state["used"] >= limit:
            return False
        state["used"] += 1
        if not _write_budget(state):
            return True
        return True


def reset_llm_budget_state() -> None:
    """Drop the one-shot unwritable warning latch (tests)."""
    global _budget_unwritable_logged
    with _budget_lock:
        _budget_unwritable_logged = False


# --- the single LLM call ------------------------------------------------------

def complete_with_backoff(prompt: str) -> str:
    """Exactly one LLM completion (per user question), with tenacity backoff."""
    bundle = get_bundle()
    if bundle.provider != "gemini" or bundle.llm is None:
        raise ProviderError("LLM unavailable in retrieval-only mode")

    @_with_backoff
    def _complete() -> str:
        return str(bundle.llm.complete(prompt))

    try:
        return _complete()
    except Exception as exc:  # noqa: BLE001 - normalized to typed errors
        if _is_retryable(exc):
            raise RateLimitedError(retry_after_s=_parse_retry_after_s(exc)) from exc
        raise ProviderError("LLM request failed") from exc
