"""Settings & paths — the only reader of `.env` (pydantic-settings).

Twelve environment variables (CONTRACTS.md §5 — five from v1.1 plus the seven
deployment variables added in v1.2); every other port and path stays a code
constant. `GOOGLE_API_KEY` is never logged and never appears in errors, and
`ACCESS_CODE` is never logged either (§5.2).

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
import os
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

# Daily LLM budget counter (§1.11) — derived data, never a corruption source.
LLM_BUDGET_PATH = STORAGE_DIR / "llm_budget.json"

# Read-only seed corpus for AUTO_SEED (§2 ingest.seed_sample_data).
SAMPLE_DATA_DIR = BACKEND_DIR / "sample_data"

# --- Deployment constants (§2 config.py, §5) ---------------------------------

DEFAULT_PORT = 8000
LOCAL_HOST = "127.0.0.1"   # no PORT in the environment: never expose the LAN
DEPLOY_HOST = "0.0.0.0"    # a PaaS dictated PORT: bind every interface
RATE_LIMIT_WINDOW_S = 60
RATE_LIMIT_MAX_TRACKED_IPS = 4096
DEFAULT_CORS_ORIGIN = "http://localhost:3000"

# Request-body ceiling for every non-upload /api route. A JSON question is at
# most 2000 characters (§1.6); 1 MB is four orders of magnitude of slack and
# still bounds what an unauthenticated caller can make the server buffer.
MAX_JSON_BODY_BYTES = 1024 * 1024

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


def parse_cors_origins(raw: str) -> list[str]:
    """Split `CORS_ORIGINS` on commas; empty result falls back to the default.

    A literal `*` is permitted (no credentials are ever sent) but the caller
    logs one WARNING when it is used.
    """
    origins = [part.strip() for part in strip_inline_comment(raw or "").split(",")]
    origins = [o for o in origins if o]
    return origins or [DEFAULT_CORS_ORIGIN]


def bind_host() -> str:
    """`0.0.0.0` iff PORT is present in the environment, else `127.0.0.1`.

    Presence — not value — is the signal: a PaaS that dictates the port needs
    every interface bound; local dev must not start exposing itself to the LAN
    as a side effect of deployment support (resolution 15).
    """
    return DEPLOY_HOST if os.environ.get("PORT") else LOCAL_HOST


# Bounds for the integer settings: field -> (low, high). Malformed or
# out-of-range values fall back to the field default with one warning.
_INT_BOUNDS = {
    "port": (1, 65535),
    "daily_llm_budget": (0, None),
    "rate_limit_per_min": (0, None),
    "max_documents": (1, None),
    "trusted_proxy_hops": (0, 16),
}


class Settings(BaseSettings):
    """The twelve environment variables from backend/.env (CONTRACTS.md §5)."""

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

    # --- v1.2 deployment variables (§5) --------------------------------------
    port: int = DEFAULT_PORT
    cors_origins: str = DEFAULT_CORS_ORIGIN
    access_code: str = ""
    daily_llm_budget: int = 200
    rate_limit_per_min: int = 10
    max_documents: int = 50
    auto_seed: Literal["on", "off"] = "on"
    # How many RIGHTMOST X-Forwarded-For entries were written by proxies we
    # control. 0 (the default) ignores the header entirely and keys on the
    # socket peer. Only a trusted proxy can write the rightmost N entries;
    # everything left of them is attacker-supplied (§1.10, ruled r3).
    trusted_proxy_hops: int = 0

    @field_validator(
        "provider",
        "gemini_llm_model",
        "gemini_embed_model",
        "rerank",
        "cors_origins",
        "auto_seed",
        mode="before",
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

    @field_validator(
        "port",
        "daily_llm_budget",
        "rate_limit_per_min",
        "max_documents",
        "trusted_proxy_hops",
        mode="before",
    )
    @classmethod
    def _decomment_int(cls, value, info):
        """De-comment and coerce; malformed/out-of-range => default + warning.

        A deployment must never fail to boot because a dashboard field holds
        `10 # per minute` or a nonsense port — the documented default is always
        a safe posture (§5).
        """
        field = cls.model_fields.get(info.field_name)
        default = field.default if field is not None else 0
        if isinstance(value, bool):
            return default
        if isinstance(value, str):
            cleaned = strip_inline_comment(value)
            if cleaned == "":
                return default
            try:
                value = int(cleaned)
            except ValueError:
                logger.warning(
                    "%s is not an integer; falling back to %s",
                    info.field_name.upper(),
                    default,
                )
                return default
        if not isinstance(value, int):
            return default
        low, high = _INT_BOUNDS.get(info.field_name, (None, None))
        if (low is not None and value < low) or (high is not None and value > high):
            logger.warning(
                "%s=%s is out of range (%s..%s); falling back to %s",
                info.field_name.upper(),
                value,
                low,
                "-" if high is None else high,
                default,
            )
            return default
        return value

    @field_validator("access_code", mode="before")
    @classmethod
    def _decomment_access_code(cls, value):
        """De-comment per §5 (r3) — but never silently.

        A dashboard has no comment syntax, so an operator-chosen code holding
        ` #` is truncated and one starting with `#` collapses to empty, which
        turns the gate OFF while the operator believes it is armed. The value
        is still de-commented (the contract says every value is), but any
        change is reported. The code itself is NEVER logged.
        """
        if not isinstance(value, str):
            return value
        raw = value.strip()
        cleaned = strip_inline_comment(value)
        if cleaned != raw:
            if cleaned == "":
                logger.warning(
                    "ACCESS_CODE looks like a comment (it starts with '#') and was "
                    "read as empty -- THE ACCESS GATE IS OFF. Choose a code with no "
                    "'#'. The value is never logged."
                )
            else:
                logger.warning(
                    "ACCESS_CODE contained ' #' and was truncated at it (%d of %d "
                    "characters kept). Choose a code with no '#'. The value is "
                    "never logged.",
                    len(cleaned), len(raw),
                )
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
