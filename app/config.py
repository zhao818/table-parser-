"""
Application configuration via environment variables.

Two-stage pipeline (recommended):
    Stage 1: Qwen VL reads image → raw table text
    Stage 2: DeepSeek structures text → JSON with rowspan/colspan

Single-model fallback:
    Set PIPELINE_MODE=single, uses one multimodal model for everything.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict

# ---------------------------------------------------------------------------
# Provider presets
# ---------------------------------------------------------------------------

PROVIDER_PRESETS: dict[str, dict[str, str]] = {
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
    },
    "qwen": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-vl-max",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o",
    },
}


class Settings(BaseSettings):
    """Application settings loaded from environment / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Pipeline Mode ---
    pipeline_mode: str = "dual"  # "dual" = Qwen VL + DeepSeek, "single" = one model

    # --- Stage 1: Vision (reads image → raw text) ---
    vision_api_key: str = ""
    vision_base_url: str = ""
    vision_model: str = ""

    # --- Stage 2: Reasoning (structures text → JSON) ---
    reasoning_api_key: str = ""
    reasoning_base_url: str = ""
    reasoning_model: str = ""

    # --- Single-model fallback ---
    llm_provider: str = "qwen"
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_model: str = ""
    llm_max_tokens: int = 4096
    llm_temperature: float = 0.1
    llm_timeout: int = 120

    # --- OCR Preprocessing ---
    ocr_enabled: bool = True
    ocr_languages: list[str] = ["ch_sim", "en"]

    # --- Server ---
    host: str = "0.0.0.0"
    port: int = 9000
    debug: bool = False
    cors_origins: list[str] = ["*"]

    # --- Upload Limits ---
    max_upload_size_mb: int = 20
    allowed_image_types: list[str] = [
        "image/png", "image/jpeg", "image/webp", "image/tiff",
    ]

    # --- Caching ---
    cache_enabled: bool = True
    cache_max_entries: int = 500
    cache_ttl_seconds: int = 3600

    # --- Logging ---
    log_level: str = "INFO"
    log_format: str = "text"

    # --- Image Preprocessing ---
    # Qwen VL handles up to ~6000px on the long side before detail loss.
    # We keep defaults high so small text and fine layout are preserved.
    # Only truly oversized images (>6K) are downscaled.
    image_max_width: int = 4096
    image_max_height: int = 4096
    image_min_dpi: int = 150  # warn if image DPI is below this
    image_quality: int = 85

    # ------------------------------------------------------------------
    # Computed properties
    # ------------------------------------------------------------------

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_size_mb * 1024 * 1024

    def _resolve(self, explicit: str, provider: str, field: str, default: str) -> str:
        if explicit:
            return explicit
        preset = PROVIDER_PRESETS.get(provider, {})
        return preset.get(field, default)

    @property
    def resolved_vision_url(self) -> str:
        return self.vision_base_url or self._resolve("", "qwen", "base_url", "")

    @property
    def resolved_vision_model(self) -> str:
        return self.vision_model or self._resolve("", "qwen", "model", "qwen-vl-max")

    @property
    def resolved_reasoning_url(self) -> str:
        return self.reasoning_base_url or self._resolve("", "deepseek", "base_url", "")

    @property
    def resolved_reasoning_model(self) -> str:
        return self.reasoning_model or self._resolve("", "deepseek", "model", "deepseek-chat")

    @property
    def resolved_base_url(self) -> str:
        if self.llm_base_url:
            return self.llm_base_url
        preset = PROVIDER_PRESETS.get(self.llm_provider, {})
        return preset.get("base_url", "")

    @property
    def resolved_model(self) -> str:
        if self.llm_model:
            return self.llm_model
        preset = PROVIDER_PRESETS.get(self.llm_provider, {})
        return preset.get("model", "")


@lru_cache
def get_settings() -> Settings:
    return Settings()


PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
STATIC_DIR: Path = PROJECT_ROOT / "static"
