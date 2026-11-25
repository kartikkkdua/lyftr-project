"""Application settings, read once from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _list(name: str, default: str = "") -> list[str]:
    raw = os.getenv(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass(frozen=True)
class KeySlot:
    """One entry of the key pool. `key_id` is the only thing ever exposed to clients."""

    key_id: str
    api_key: str = field(repr=False)
    model: str


@dataclass(frozen=True)
class Settings:
    keys: list[KeySlot]
    database_url: str
    allowed_origins: list[str]
    enable_failure_simulation: bool
    cache_enabled: bool
    cache_type: str
    cache_dir: str
    cache_ttl_seconds: int
    default_system_prompt: str
    temperature: float
    max_output_tokens: int
    request_timeout_seconds: float
    history_limit: int
    api_base: str | None

    @property
    def database_path(self) -> str:
        return sqlite_path_from_url(self.database_url)


def sqlite_path_from_url(url: str) -> str:
    """`sqlite:////data/app.db` -> `/data/app.db`, `sqlite:///./app.db` -> `./app.db`."""
    prefix = "sqlite:///"
    if url.startswith(prefix):
        return url[len(prefix):] or ":memory:"
    if url.startswith("sqlite://"):
        return ":memory:"
    # Allow a bare filesystem path as a convenience.
    return url


def load_settings() -> Settings:
    api_keys = _list("LLM_API_KEYS")
    default_model = os.getenv("LLM_MODEL", "gemini/gemini-3.5-flash-lite").strip()
    # Optional per-key models, aligned with LLM_API_KEYS. Missing entries use LLM_MODEL.
    per_key_models = _list("LLM_MODELS")

    keys = [
        KeySlot(
            key_id=f"key_{i + 1}",
            api_key=key,
            model=per_key_models[i] if i < len(per_key_models) else default_model,
        )
        for i, key in enumerate(api_keys)
    ]

    return Settings(
        keys=keys,
        database_url=os.getenv("DATABASE_URL", "sqlite:///./data/app.db"),
        allowed_origins=_list("ALLOWED_ORIGINS", "http://localhost:3000"),
        enable_failure_simulation=_bool("ENABLE_FAILURE_SIMULATION", False),
        cache_enabled=_bool("CACHE_ENABLED", True),
        cache_type=os.getenv("CACHE_TYPE", "disk").strip().lower(),
        cache_dir=os.getenv("CACHE_DIR", "./data/llm_cache"),
        cache_ttl_seconds=int(os.getenv("CACHE_TTL_SECONDS", "86400")),
        default_system_prompt=os.getenv(
            "DEFAULT_SYSTEM_PROMPT", "You are a helpful, concise assistant."
        ),
        temperature=float(os.getenv("LLM_TEMPERATURE", "0.2")),
        max_output_tokens=int(os.getenv("LLM_MAX_OUTPUT_TOKENS", "1024")),
        request_timeout_seconds=float(os.getenv("LLM_TIMEOUT_SECONDS", "30")),
        history_limit=int(os.getenv("HISTORY_LIMIT", "40")),
        api_base=os.getenv("LLM_API_BASE") or None,
    )
