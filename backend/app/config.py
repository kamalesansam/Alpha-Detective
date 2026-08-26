"""Settings & paths — the only reader of `.env` (pydantic-settings).

Exactly five environment variables (see CONTRACTS.md §5); ports and paths are
code constants. `GOOGLE_API_KEY` is never logged and never appears in errors.

Two hygiene rules are enforced here, both ratified in round 3:

1. Inline comments are stripped from every value before validation.
   python-dotenv only strips a trailing `# ...` when a real value precedes it;
   for an *empty* value (`KEY=   # note`) the comment becomes the value. That
   shape used to ship in `.env.example`, so a copied template produced a
   76-character "key" starting with `#`.
2. `GOOGLE_API_KEY` is sanity-checked and treated as UNSET when it cannot
   possibly be a key. A malformed key otherwise reaches an HTTP auth header,
   where a non-ASCII byte raises UnicodeEncodeError deep inside the transport
   and surfaces as a misleading "check the API key and network" error.
"""

from __future__ import annotations

import logging
import re
import string
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_core import PydanticUndefined
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger("alpha.config")

# --- Path constants (all under backend/storage/) -----------------------------

BACKEND_DIR = Path(__file__).resolve().parent.parent  # .../backend
STORAGE_DIR = BACKEND_DIR / "storage"
UPLOADS_DIR = STORAGE_DIR / "uploads"
CHROMA_DIR = STORAGE_DIR / "chroma"
DOCSTORE_PATH = STORAGE_DIR / "docstore.json"
MANIFEST_PATH = STORAGE_DIR / "manifest.json"
EMBED_CACHE_PATH = STORAGE_DIR / "embed_cache.json"

# Local reranker model cache (first-run download target; derived data).
RERANK_MODEL_DIR = STORAGE_DIR / "models"

# --- Value hygiene -----------------------------------------------------------

_TRAILING_COMMENT = re.compile(r"\s+#.*$", re.DOTALL)
_PRINTABLE = set(string.printable) - set("\t\n\r\x0b\x0c")


def strip_inline_comment(raw: str) -> str:
    """Return `raw` without a trailing `# ...` comment, whitespace-trimmed.

    A value that is *entirely* a comment collapses to "" so the field default
    applies instead of an enum failure.
    """
    value = raw.strip()
    if value.startswith("#"):
        return ""
    return _TRAILING_COMMENT.sub("", value).strip()


def key_is_implausible(key: str) -> bool:
    """True when `key` cannot be a credential and must be treated as UNSET.

    Never called with, and never returns, key material.
    """
    if not key:
        return True
    if key.startswith("#") or "#" in key:
        return True
    if any(c.isspace() for c in key):
        return True
    if not key.isascii():
        return True
    return any(c not in _PRINTABLE for c in key)


class Settings(BaseSettings):
    """The five environment variables from backend/.env (CONTRACTS.md §5)."""

    model_config = SettingsConfigDict(
        env_file=str(BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    google_api_key: str = ""
    provider: Literal["auto", "gemini", "none"] = "auto"
    gemini_llm_model: str = "auto"
    gemini_embed_model: str = "auto"
    rerank: Literal["on", "off"] = "on"

    @field_validator(
        "provider", "gemini_llm_model", "gemini_embed_model", "rerank", mode="before"
    )
    @classmethod
    def _decomment(cls, value, info):
        """Strip inline comments; a comment-only value falls back to the default.

        Without this, `PROVIDER=   # note` reaches the Literal as "# note" and
        raises a validation error that reads like the operator typed nonsense.
        """
        if not isinstance(value, str):
            return value
        cleaned = strip_inline_comment(value)
        if cleaned == "":
            field = cls.model_fields.get(info.field_name)
            if field is not None and field.default is not PydanticUndefined:
                return field.default
        return cleaned

    @field_validator("google_api_key", mode="before")
    @classmethod
    def _clean_key(cls, value):
        """Strip a trailing comment, then drop the key if it cannot be valid.

        Warns exactly once and never logs the value or any slice of it. The
        common miss is a template line whose whole value is a comment; that
        collapses to empty here and is reported rather than silently ignored.
        """
        if not isinstance(value, str):
            return value
        raw = value.strip()
        if not raw:
            return ""
        cleaned = strip_inline_comment(value)
        if cleaned == "" or key_is_implausible(cleaned):
            logger.warning(
                "GOOGLE_API_KEY in backend/.env is malformed (%d characters; "
                "fails the plausibility check) and is being ignored -- starting "
                "in retrieval-only mode. Put the key alone on its line with no "
                "trailing comment. The value itself is never logged.",
                len(raw),
            )
            return ""
        return cleaned

    @property
    def effective_provider(self) -> str:
        """`auto` resolves to gemini iff a key is present, else none."""
        if self.provider == "auto":
            return "gemini" if self.google_api_key.strip() else "none"
        return self.provider

    def validate_runtime(self) -> None:
        """Raise with a clear (key-free) message on impossible configurations.

        Called during startup; main.py turns the failure into CRITICAL + exit 1.
        """
        if self.provider == "gemini" and not self.google_api_key.strip():
            raise ValueError(
                "PROVIDER=gemini but GOOGLE_API_KEY is empty or malformed. Add a "
                "valid key to backend/.env (https://aistudio.google.com/apikey), "
                "alone on its line with no trailing comment, or set PROVIDER=none "
                "for retrieval-only mode."
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def ensure_storage_dirs() -> None:
    """Create the storage skeleton (idempotent)."""
    for d in (STORAGE_DIR, UPLOADS_DIR):
        d.mkdir(parents=True, exist_ok=True)
