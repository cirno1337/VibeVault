"""Asynchronous delivery from the spool to one or more VibeVault servers."""

from __future__ import annotations

import time
from dataclasses import dataclass

from .client import post_batch
from .config import CollectorConfig
from .spool import Spool


@dataclass
class FlushStats:
    sent: int = 0
    created: int = 0
    duplicate: int = 0
    skipped: int = 0
    rejected: int = 0
    conflict: int = 0
    failed_dests: int = 0


def backoff(failures: int) -> float:
    return min(3600.0, 5.0 * 2 ** min(failures, 10))


def flush(cfg: CollectorConfig, spool: Spool, *, now=time.time, send=post_batch) -> FlushStats:
    stats = FlushStats()
    for dest in cfg.destinations:
        state = spool.dest_state(dest.name)
        if now() < state["next_try"]:
            continue
        token = dest.token()
        if not token:
            spool.set_dest_state(dest.name, state["failures"] + 1, now() + 300, "no_token_configured")
            spool.audit("flush_error", dest=dest.name, error="no_token_configured")
            stats.failed_dests += 1
            continue
        size = state["batch_size"] or cfg.batch_size
        while True:
            batch = spool.next_batch(dest.name, size)
            if not batch:
                spool.set_dest_state(dest.name, 0, 0, None, None)
                break
            res = send(dest.url, token, [p for _, _, p in batch], cfg.request_timeout)
            if res.ok and res.body:
                items = res.body.get("results", [])
                for (_, key, _), item in zip(batch, items, strict=False):
                    if item.get("status") == "rejected":
                        stats.rejected += 1
                        spool.db.execute("INSERT INTO rejected(dest,event_key,reason,ts) VALUES (?,?,?,?)", (dest.name, key, item.get("reason"), now()))
                spool.delete_outbox([i for i, _, _ in batch])  # every status is terminal; retries won't help
                for k in ("created", "duplicate", "skipped", "conflict"):
                    setattr(stats, k, getattr(stats, k) + int(res.body.get(k, 0)))
                stats.sent += len(batch)
                spool.audit("flush", dest=dest.name, sent=len(batch), **{k: res.body.get(k) for k in ("created", "duplicate", "skipped", "conflict", "rejected")})
                continue
            fails = state["failures"] + 1
            if res.status == 413 and size > 1:
                size = max(1, len(batch) // 2)
                spool.set_dest_state(dest.name, state["failures"], 0, "http_413", size)
                continue
            if res.status in (401, 403):
                wait = 900.0
            elif res.status == 413:  # single event too large: dead-letter it
                spool.delete_outbox([batch[0][0]])
                spool.db.execute("INSERT INTO rejected(dest,event_key,reason,ts) VALUES (?,?,?,?)", (dest.name, batch[0][1], "http_413_single_event", now()))
                continue
            else:
                wait = backoff(fails)
            spool.set_dest_state(dest.name, fails, now() + wait, res.error, size)
            spool.audit("flush_error", dest=dest.name, error=res.error, status=res.status, retry_in=wait)
            stats.failed_dests += 1
            break
    return stats
