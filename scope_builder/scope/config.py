"""Scope Builder configuration (environment variables or the repo-root .env)."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]
SAMPLES_DIR = Path(__file__).resolve().parents[1] / "data" / "transcripts"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=REPO_ROOT / ".env", extra="ignore")

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
    llm_timeout_seconds: float = 120.0

    data_dir: Path = Path("data")
    scope_db_path: Path | None = None
    brain_url: str = "http://127.0.0.1:8001"
    brain_timeout_seconds: float = 60.0
    brain_concurrency: int = 3

    # Transcripts longer than this are processed in overlapping segments (map) and merged (reduce).
    long_transcript_chars: int = 30_000
    segment_chars: int = 12_000
    indicative_hourly_rate: float | None = None
    company_name: str = "Brilliant Directories"

    @property
    def db_path(self) -> Path:
        path = Path(self.scope_db_path) if self.scope_db_path else Path(self.data_dir) / "scope.db"
        return path if path.is_absolute() else REPO_ROOT / path

    @property
    def model_prices(self) -> dict[str, tuple[float, float]]:
        try:
            raw = json.loads(self.model_prices_json or "{}")
            return {k: (float(v[0]), float(v[1])) for k, v in raw.items()}
        except (ValueError, TypeError, IndexError):
            return {}


@lru_cache
def get_settings() -> Settings:
    return Settings()
