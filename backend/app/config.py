"""Settings & paths — the only reader of `.env` (pydantic-settings).

Exactly five environment variables (see CONTRACTS.md §5); ports and paths are
code constants. `GOOGLE_API_KEY` is never logged and never appears in errors.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

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
                "PROVIDER=gemini but GOOGLE_API_KEY is empty. Add a key to "
                "backend/.env (https://aistudio.google.com/apikey) or set "
                "PROVIDER=none for retrieval-only mode."
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def ensure_storage_dirs() -> None:
    """Create the storage skeleton (idempotent)."""
    for d in (STORAGE_DIR, UPLOADS_DIR):
        d.mkdir(parents=True, exist_ok=True)
