"""OpenAI-compatible chat-completions client (llama.cpp server, Groq, Gemini's compat endpoint).

NOT exercised against the real services in the cloud dev environment; covered by tests using
httpx's MockTransport only. Verify on first real use (see docs/providers.md).
"""

from __future__ import annotations

import httpx

from .base import (
    CompletionRequest,
    CompletionResponse,
    ProviderBadResponse,
    ProviderRateLimited,
    ProviderRequestTooLarge,
    ProviderUnavailable,
)


class OpenAICompatProvider:
    def __init__(
        self,
        name: str,
        base_url: str | None,
        model: str | None,
        api_key: str | None = None,
        is_local: bool = False,
        timeout: float = 60.0,
        transport: httpx.BaseTransport | None = None,
        extra_body: dict | None = None,
    ):
        self.name = name
        self.base_url = (base_url or "").rstrip("/")
        self.model = model
        self._api_key = api_key
        self.is_local = is_local
        self._extra_body = dict(extra_body or {})  # provider-specific request fields, e.g. DeepSeek "thinking"
        self._client = httpx.Client(timeout=timeout, transport=transport)

    def configured(self) -> bool:
        if not self.base_url or not self.model:
            return False
        return self.is_local or bool(self._api_key)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}

    def check_ready(self) -> bool:
        try:
            r = self._client.get(f"{self.base_url}/models", headers=self._headers(), timeout=5.0)
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    def complete(self, req: CompletionRequest) -> CompletionResponse:
        body: dict = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": req.system},
                {"role": "user", "content": req.user},
            ],
            "temperature": 0,
            "max_tokens": req.max_tokens,
        }
        if req.json_output:
            body["response_format"] = {"type": "json_object"}
        body.update(self._extra_body)
        try:
            r = self._client.post(f"{self.base_url}/chat/completions", json=body, headers=self._headers())
        except httpx.TimeoutException as e:
            raise ProviderUnavailable(f"{self.name}: timeout") from e
        except httpx.HTTPError as e:
            raise ProviderUnavailable(f"{self.name}: {type(e).__name__}") from e
        if r.status_code == 429:
            try:
                ra = float(r.headers.get("retry-after", "60"))
            except ValueError:
                ra = 60.0
            raise ProviderRateLimited(min(max(ra, 1.0), 6 * 3600))
        if r.status_code == 413:
            raise ProviderRequestTooLarge(f"{self.name}: HTTP 413 (request too large)")
        if r.status_code >= 500 or r.status_code in (401, 403, 404):
            raise ProviderUnavailable(f"{self.name}: HTTP {r.status_code}")
        if r.status_code >= 400:
            raise ProviderBadResponse(f"{self.name}: HTTP {r.status_code}")
        try:
            data = r.json()
            text = data["choices"][0]["message"]["content"]
            usage = data.get("usage") or {}
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise ProviderBadResponse(f"{self.name}: unexpected response shape") from e
        return CompletionResponse(
            text=text or "",
            model=str(data.get("model") or self.model),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
        )
