"""Provider interface for structured extraction / summarisation.

A provider turns a prompt into text (expected to be JSON for extraction). Everything about
*what* to extract lives in `vibevault.knowledge`; providers know nothing about the schema.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class ProviderState(StrEnum):
    DISABLED = "disabled"  # not listed in VIBEVAULT_PROVIDERS
    UNCONFIGURED = "unconfigured"  # listed but missing base URL / key / model
    BLOCKED_EXTERNAL = "blocked_external"  # cloud provider while VIBEVAULT_ALLOW_EXTERNAL_AI=false
    READY = "ready"
    RATE_LIMITED = "rate_limited"
    BUDGET_EXHAUSTED = "budget_exhausted"
    UNAVAILABLE = "unavailable"  # configured but endpoint not answering


@dataclass
class ProviderStatus:
    name: str
    state: ProviderState
    local: bool = False
    detail: str | None = None
    model: str | None = None
    requests_today: int = 0
    daily_request_budget: int | None = None
    retry_after_seconds: float | None = None

    @property
    def usable(self) -> bool:
        return self.state == ProviderState.READY

    def as_dict(self) -> dict:
        return {**self.__dict__, "state": self.state.value}


@dataclass
class CompletionRequest:
    system: str
    user: str
    json_output: bool = True
    max_tokens: int = 2048


@dataclass
class CompletionResponse:
    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0


class ProviderError(Exception):
    """Base class. Messages must never include prompt or response content."""


class ProviderRateLimited(ProviderError):
    def __init__(self, retry_after: float = 60.0):
        super().__init__(f"rate limited; retry after {retry_after:.0f}s")
        self.retry_after = retry_after


class ProviderUnavailable(ProviderError):
    pass


class ProviderBadResponse(ProviderError):
    pass


class ProviderRequestTooLarge(ProviderBadResponse):
    """HTTP 413: one request exceeds what the provider accepts (e.g. Groq's per-minute token limit).
    Retrying the same request cannot succeed; send less."""


class Provider(Protocol):
    name: str
    is_local: bool
    model: str | None

    def configured(self) -> bool: ...

    def check_ready(self) -> bool:
        """Cheap probe that the endpoint is actually reachable (e.g. llama.cpp loaded a model)."""

    def complete(self, req: CompletionRequest) -> CompletionResponse: ...
