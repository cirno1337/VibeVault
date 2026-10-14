"""Provider selection with explicit disabled/unconfigured states, budgets and cooldowns.

Environment (all optional; nothing is hardcoded and no key is ever stored in the database):
  VIBEVAULT_PROVIDERS=local,groq,gemini     ordered preference list; empty = AI disabled
  VIBEVAULT_PROVIDER_<NAME>_BASE_URL        required for "local"; presets exist for groq/gemini
  VIBEVAULT_PROVIDER_<NAME>_API_KEY         required for non-local providers
  VIBEVAULT_PROVIDER_<NAME>_MODEL           required
  VIBEVAULT_PROVIDER_<NAME>_TIMEOUT         seconds, default 60
  VIBEVAULT_PROVIDER_<NAME>_DAILY_REQUEST_BUDGET   optional integer
Non-local providers additionally require VIBEVAULT_ALLOW_EXTERNAL_AI=true.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable, Mapping
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from .. import models as m
from ..config import Settings
from .base import Provider, ProviderRateLimited, ProviderState, ProviderStatus
from .openai_compat import OpenAICompatProvider

PRESET_BASE_URLS = {
    "groq": "https://api.groq.com/openai/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "deepseek": "https://api.deepseek.com",
}
# Request fields merged into every chat-completions body. DeepSeek thinks by default, which ignores
# temperature and spends max_tokens on reasoning; these are short JSON classification calls.
PRESET_EXTRA_BODY = {
    "deepseek": {"thinking": {"type": "disabled"}},
}


def _env(env: Mapping[str, str], name: str, key: str) -> str | None:
    v = env.get(f"VIBEVAULT_PROVIDER_{name.upper()}_{key}")
    return v.strip() if v and v.strip() else None


def _extra_body(env: Mapping[str, str], name: str) -> dict:
    raw = _env(env, name, "EXTRA_BODY")  # JSON object; overrides the preset
    if raw is None:
        return dict(PRESET_EXTRA_BODY.get(name, {}))
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"VIBEVAULT_PROVIDER_{name.upper()}_EXTRA_BODY must be a JSON object")
    return data


def build_provider(name: str, env: Mapping[str, str]) -> OpenAICompatProvider:
    return OpenAICompatProvider(
        name=name,
        base_url=_env(env, name, "BASE_URL") or PRESET_BASE_URLS.get(name),
        model=_env(env, name, "MODEL"),
        api_key=_env(env, name, "API_KEY"),
        is_local=(name == "local"),
        timeout=float(_env(env, name, "TIMEOUT") or 60),
        extra_body=_extra_body(env, name),
    )


class ProviderRegistry:
    def __init__(
        self,
        settings: Settings,
        env: Mapping[str, str] | None = None,
        providers: dict[str, Provider] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.settings = settings
        self.env = env if env is not None else os.environ
        self._providers: dict[str, Provider] = providers or {}
        self._cooldown: dict[str, float] = {}
        self._clock = clock
        self._lock = threading.Lock()

    def names(self) -> list[str]:
        return self.settings.provider_names()

    def _get(self, name: str) -> Provider:
        if name not in self._providers:
            self._providers[name] = build_provider(name, self.env)
        return self._providers[name]

    def _budget(self, name: str) -> int | None:
        raw = _env(self.env, name, "DAILY_REQUEST_BUDGET")
        return int(raw) if raw and raw.isdigit() else None

    @staticmethod
    def _today(now: datetime) -> str:
        return now.strftime("%Y-%m-%d")

    def requests_today(self, db: DbSession, name: str, now: datetime) -> int:
        row = db.scalar(select(m.ProviderUsage).where(m.ProviderUsage.provider == name, m.ProviderUsage.day == self._today(now)))
        return row.requests if row else 0

    def status(self, db: DbSession, name: str, now: datetime, probe: bool = False) -> ProviderStatus:
        if name not in self.names():
            return ProviderStatus(name, ProviderState.DISABLED, detail="not listed in VIBEVAULT_PROVIDERS")
        p = self._get(name)
        base = dict(name=name, local=p.is_local, model=p.model)
        if not p.configured():
            return ProviderStatus(**base, state=ProviderState.UNCONFIGURED, detail="missing base URL, model or API key")
        if not p.is_local and not self.settings.allow_external_ai:
            return ProviderStatus(**base, state=ProviderState.BLOCKED_EXTERNAL, detail="set VIBEVAULT_ALLOW_EXTERNAL_AI=true to permit sending data to external providers")
        used, budget = self.requests_today(db, name, now), self._budget(name)
        base.update(requests_today=used, daily_request_budget=budget)
        if budget is not None and used >= budget:
            return ProviderStatus(**base, state=ProviderState.BUDGET_EXHAUSTED, detail="daily request budget used up")
        remaining = self._cooldown.get(name, 0) - self._clock()
        if remaining > 0:
            return ProviderStatus(**base, state=ProviderState.RATE_LIMITED, retry_after_seconds=remaining)
        if probe and not p.check_ready():
            return ProviderStatus(**base, state=ProviderState.UNAVAILABLE, detail="endpoint not ready")
        return ProviderStatus(**base, state=ProviderState.READY)

    def all_status(self, db: DbSession, now: datetime) -> list[ProviderStatus]:
        return [self.status(db, n, now) for n in self.names()]

    def select(self, db: DbSession, now: datetime) -> tuple[Provider | None, list[ProviderStatus]]:
        """First usable provider in configured order (readiness-probed), plus all statuses."""
        statuses = []
        for name in self.names():
            st = self.status(db, name, now, probe=True)
            statuses.append(st)
            if st.usable:
                return self._get(name), statuses
        return None, statuses

    def any_configured(self) -> bool:
        return bool(self.names())

    def note_rate_limited(self, name: str, err: ProviderRateLimited) -> None:
        with self._lock:
            self._cooldown[name] = self._clock() + err.retry_after

    def record_usage(self, db: DbSession, name: str, now: datetime, prompt_tokens: int, completion_tokens: int) -> None:
        row = db.scalar(select(m.ProviderUsage).where(m.ProviderUsage.provider == name, m.ProviderUsage.day == self._today(now)))
        if row is None:
            row = m.ProviderUsage(provider=name, day=self._today(now), requests=0, prompt_tokens=0, completion_tokens=0)
            db.add(row)
        row.requests += 1
        row.prompt_tokens += prompt_tokens
        row.completion_tokens += completion_tokens
        db.commit()
