"""Minimal HTTP client for the ingest API (stdlib only)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass


@dataclass
class SendResult:
    ok: bool
    retryable: bool
    status: int | None = None
    body: dict | None = None
    error: str | None = None  # a short code, never payload content


def post_batch(base_url: str, token: str, events: list[dict], timeout: float = 15.0) -> SendResult:
    req = urllib.request.Request(
        base_url.rstrip("/") + "/api/v1/events/batch",
        data=json.dumps({"events": events}).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "User-Agent": "vibevault-collector/0.1"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - URL is operator-configured
            return SendResult(True, False, r.status, json.loads(r.read() or b"{}"))
    except urllib.error.HTTPError as e:
        # 401/403: credentials problem (stop and alert). 413: batch too big (caller splits). 5xx/429: retry.
        retryable = e.code >= 500 or e.code in (408, 429)
        return SendResult(False, retryable, e.code, error=f"http_{e.code}")
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
        return SendResult(False, True, None, error="network")
    except ValueError:
        return SendResult(False, True, None, error="bad_response")
