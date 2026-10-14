from __future__ import annotations

import json
import logging
import time
import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..logging_setup import request_id_var

log = logging.getLogger("vibevault.http")


class BodyTooLarge(Exception):
    pass


class RequestContextMiddleware:
    """Request id, body-size limit (also for chunked bodies), and a one-line access log.

    Only method, path, status and timing are logged; never query strings or bodies.
    """

    def __init__(self, app: ASGIApp, max_body_bytes: int):
        self.app = app
        self.max = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        rid = uuid.uuid4().hex[:12]
        token = request_id_var.set(rid)
        start = time.monotonic()
        status = {"code": 0}
        headers = dict(scope.get("headers") or [])
        declared = headers.get(b"content-length")
        too_large = bool(declared and declared.isdigit() and int(declared) > self.max)
        received = {"n": 0}

        async def limited_receive() -> Message:
            msg = await receive()
            if msg["type"] == "http.request":
                received["n"] += len(msg.get("body", b""))
                if received["n"] > self.max:
                    raise BodyTooLarge()
            return msg

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                message.setdefault("headers", []).append((b"x-request-id", rid.encode()))
            await send(message)

        async def reject() -> None:
            body = json.dumps({"error": {"code": "payload_too_large", "message": f"Request body exceeds {self.max} bytes"}}).encode()
            await send_wrapper({"type": "http.response.start", "status": 413, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
            await send_wrapper({"type": "http.response.body", "body": body})

        try:
            if too_large:
                await reject()
            else:
                try:
                    await self.app(scope, limited_receive, send_wrapper)
                except BodyTooLarge:
                    if status["code"] == 0:
                        await reject()
        finally:
            if scope["path"] not in ("/healthz",):
                log.info(
                    "request",
                    extra={"method": scope["method"], "path": scope["path"], "status": status["code"], "ms": round((time.monotonic() - start) * 1000, 1)},
                )
            request_id_var.reset(token)
