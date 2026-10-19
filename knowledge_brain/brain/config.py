"""Runtime configuration. Everything comes from environment variables or the repo-root .env file."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=REPO_ROOT / ".env", extra="ignore")

    # LLM
    llm_provider: str = "auto"
    gemini_api_key: str = ""
    gemini_model_fast: str = "gemini-3.5-flash-lite"
    gemini_model_smart: str = "gemini-3.8-flash"
    gemini_model_vision: str = "gemini-3.8-flash"
    gemini_embed_model: str = "gemini-embedding-001"
    gemini_embed_dim: int = 768
    anthropic_api_key: str = ""
    anthropic_model_fast: str = "claude-haiku-5-5"
    anthropic_model_smart: str = "claude-sonnet-5-5"
    model_prices_json: str = "{}"
    llm_timeout_seconds: float = 60.0

    # Storage
    data_dir: Path = REPO_ROOT / "data"
    brain_db_path: Path | None = None

    # Ingestion
    sitemap_url: str = "https://support.brilliantdirectories.com/support/sitemap.xml"
    crawl_url_pattern: str = "/support/solutions/articles/"
    crawl_concurrency: int = 2
    crawl_delay_seconds: float = 1.0
    crawl_max_retries: int = 3
    crawl_timeout_seconds: float = 30.0
    crawl_user_agent: str = "AITechConsultant-KnowledgeBrain/1.0 (polite crawler; 1 req/s)"
    max_images_per_article: int = 4
    max_image_bytes: int = 4_000_000
    daily_sync_enabled: bool = False
    daily_sync_hour_utc: int = 3
    daily_sync_batch_limit: int = 0
    # Refuse to mark articles removed when the sitemap suddenly shrinks below this share of known articles.
    removal_safety_ratio: float = 0.5

    # Retrieval / answering
    dense_min_score: float = 0.55
    lexical_min_coverage: float = 0.6
    top_k: int = 6

    brain_admin_token: str = ""
    company_name: str = "Brilliant Directories"

    @field_validator("brain_db_path", mode="before")
    @classmethod
    def _blank_is_none(cls, v: object) -> object:
        return None if isinstance(v, str) and not v.strip() else v

    @property
    def db_path(self) -> Path:
        if str(self.brain_db_path) == ":memory:":
            return Path(":memory:")
        if self.brain_db_path:
            return _resolve(Path(self.brain_db_path))
        return _resolve(Path(self.data_dir)) / "brain.db"

    @property
    def model_prices(self) -> dict[str, tuple[float, float]]:
        try:
            raw = json.loads(self.model_prices_json or "{}")
            return {k: (float(v[0]), float(v[1])) for k, v in raw.items()}
        except (ValueError, TypeError, IndexError):
            return {}


def _resolve(path: Path) -> Path:
    """Relative paths in .env are relative to the repo root, not the process cwd."""
    return path if path.is_absolute() else REPO_ROOT / path


@lru_cache
def get_settings() -> Settings:
    return Settings()
