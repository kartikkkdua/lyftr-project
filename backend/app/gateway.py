"""LLM gateway: LiteLLM calls over an ordered key pool, with failover and response caching.

Flow for one request:

1. Walk the key pool in order (key_1, key_2, ...).
2. For each key, call `litellm.acompletion(..., caching=True)`. LiteLLM first looks the
   request up in its cache (key = model + messages + sampling params; the API key is NOT part
   of it), so a hit is returned without touching the provider.
3. If the provider (or the X-Simulate-Failure hook) fails with 429 / quota / 5xx / timeout /
   connection error / auth error, record it and move to the next key.
4. If every key fails, raise `AllProvidersUnavailable` (the API turns it into a 503).
"""

from __future__ import annotations

import logging
import os
import warnings
from dataclasses import dataclass
from typing import Any

import litellm
from litellm.caching.caching import Cache

from .config import KeySlot, Settings

logger = logging.getLogger("gateway")

litellm.suppress_debug_info = True
# LiteLLM's cached-response objects trigger noisy (harmless) pydantic serializer warnings.
warnings.filterwarnings("ignore", message="Pydantic serializer warnings")
litellm.drop_params = True  # ignore params a given provider does not support

SIMULATABLE_STATUSES = {429, 500, 502, 503}


class AllProvidersUnavailable(Exception):
    """Every key in the pool failed for this request."""

    def __init__(self, failures: list[dict[str, Any]]):
        super().__init__("all providers unavailable")
        self.failures = failures


@dataclass
class CompletionResult:
    reply: str
    model: str
    key_id: str | None
    fallback_attempts: int
    input_tokens: int
    output_tokens: int
    cache_hit: bool


def configure_cache(settings: Settings) -> bool:
    """Enable LiteLLM's built-in response cache. Returns whether caching is on."""
    if not settings.cache_enabled:
        litellm.cache = None
        return False
    if settings.cache_type == "disk":
        os.makedirs(settings.cache_dir, exist_ok=True)
        litellm.cache = Cache(
            type="disk", disk_cache_dir=settings.cache_dir, ttl=settings.cache_ttl_seconds
        )
    else:
        litellm.cache = Cache(type="local", ttl=settings.cache_ttl_seconds)
    return True


def parse_simulation_header(raw: str | None, key_ids: list[str]) -> dict[str, int]:
    """Parse `X-Simulate-Failure` into {key_id: status}.

    Format: comma-separated `<key_id>=<status>` pairs, `*` means every key. Malformed pairs,
    unknown key ids and unsupported statuses are ignored rather than failing the request.
    """
    if not raw:
        return {}
    plan: dict[str, int] = {}
    for part in raw.split(","):
        if "=" not in part:
            continue
        target, _, status_text = part.partition("=")
        target, status_text = target.strip(), status_text.strip()
        try:
            status = int(status_text)
        except ValueError:
            continue
        if status not in SIMULATABLE_STATUSES:
            continue
        if target == "*":
            for key_id in key_ids:
                plan[key_id] = status
        elif target in key_ids:
            plan[target] = status
    return plan


def simulated_error(status: int, slot: KeySlot) -> Exception:
    """The same LiteLLM exception type a real provider failure with `status` would raise."""
    message = f"simulated HTTP {status} for {slot.key_id} (X-Simulate-Failure)"
    kwargs = {"message": message, "llm_provider": "simulated", "model": slot.model}
    if status == 429:
        return litellm.RateLimitError(**kwargs)
    if status == 503:
        return litellm.ServiceUnavailableError(**kwargs)
    if status == 502:
        return litellm.APIError(status_code=502, **kwargs)
    return litellm.InternalServerError(**kwargs)


def describe_error(exc: Exception) -> tuple[int | None, str]:
    """(status, short reason) for logging. Never includes the API key."""
    status = getattr(exc, "status_code", None)
    if isinstance(exc, litellm.RateLimitError):
        reason = "rate_limited_or_quota"
    elif isinstance(exc, litellm.Timeout):
        reason = "timeout"
    elif isinstance(exc, litellm.APIConnectionError):
        reason = "connection_error"
    elif isinstance(exc, (litellm.exceptions.AuthenticationError, litellm.exceptions.PermissionDeniedError)):
        reason = "auth_error"
    elif isinstance(status, int) and status >= 500:
        reason = "server_error"
    else:
        reason = type(exc).__name__
    return (status if isinstance(status, int) else None), reason


def _usage_tokens(response: Any, model: str, messages: list[dict], reply: str) -> tuple[int, int]:
    usage = getattr(response, "usage", None)
    prompt = getattr(usage, "prompt_tokens", None) if usage else None
    completion = getattr(usage, "completion_tokens", None) if usage else None
    if prompt is None:
        prompt = litellm.token_counter(model=model, messages=messages)
    if completion is None:
        completion = litellm.token_counter(model=model, text=reply)
    return int(prompt), int(completion)


class LLMGateway:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.keys = settings.keys
        self.cache_enabled = configure_cache(settings)

    @property
    def key_ids(self) -> list[str]:
        return [slot.key_id for slot in self.keys]

    async def complete(
        self, messages: list[dict[str, str]], simulate: dict[str, int] | None = None
    ) -> CompletionResult:
        simulate = simulate or {}
        # A request that exercises the failover hook must really go to the providers, otherwise
        # a cached answer would hide the failover we are trying to demonstrate.
        read_cache = self.cache_enabled and not simulate
        failures: list[dict[str, Any]] = []

        for slot in self.keys:
            try:
                if slot.key_id in simulate:
                    raise simulated_error(simulate[slot.key_id], slot)
                response = await litellm.acompletion(
                    model=slot.model,
                    messages=messages,
                    api_key=slot.api_key,
                    api_base=self.settings.api_base,
                    temperature=self.settings.temperature,
                    max_tokens=self.settings.max_output_tokens,
                    timeout=self.settings.request_timeout_seconds,
                    num_retries=0,  # failover is ours: move to the next key instead of retrying
                    caching=self.cache_enabled,
                    cache={"no-cache": not read_cache},
                )
                reply = (response.choices[0].message.content or "").strip()
                if not reply:
                    raise litellm.InternalServerError(
                        message="provider returned an empty reply",
                        llm_provider="unknown",
                        model=slot.model,
                    )
            except Exception as exc:  # noqa: BLE001 - every provider failure means "try next key"
                status, reason = describe_error(exc)
                failures.append({"key_id": slot.key_id, "status": status, "reason": reason})
                logger.warning(
                    "key %s failed (status=%s, reason=%s); failing over", slot.key_id, status, reason
                )
                continue

            cache_hit = bool((getattr(response, "_hidden_params", None) or {}).get("cache_hit"))
            input_tokens, output_tokens = _usage_tokens(response, slot.model, messages, reply)
            return CompletionResult(
                reply=reply,
                model=slot.model,
                key_id=None if cache_hit else slot.key_id,
                fallback_attempts=0 if cache_hit else len(failures),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_hit=cache_hit,
            )

        raise AllProvidersUnavailable(failures)
