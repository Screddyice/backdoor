"""Accept RFC 6749 Basic client authentication at the MCP SDK token endpoint.

The pinned SDK requires ``client_id`` in the form body even when the Basic header
already carries it. Composio sends the identifier only in Basic, as OAuth allows.
Keep the SDK's client-secret validation and grant checks; supply only the missing
form identifier before its token handler reads the request.
"""

from __future__ import annotations

import base64
import binascii
from urllib.parse import parse_qsl, unquote, urlencode

from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

MAX_TOKEN_BODY_BYTES = 64 * 1024


class BasicClientIdTokenMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") != "/token" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        auth = headers.get(b"authorization", b"")
        content_type = headers.get(b"content-type", b"").lower()
        if not auth.startswith(b"Basic ") or not content_type.startswith(b"application/x-www-form-urlencoded"):
            await self.app(scope, receive, send)
            return

        try:
            decoded = base64.b64decode(auth[6:], validate=True).decode("utf-8")
            encoded_client_id, separator, _ = decoded.partition(":")
            client_id = unquote(encoded_client_id)
        except (ValueError, UnicodeDecodeError, binascii.Error):
            client_id = ""
            separator = ""
        if not separator or not client_id:
            await self.app(scope, receive, send)
            return

        chunks: list[bytes] = []
        size = 0
        while True:
            event = await receive()
            if event["type"] == "http.disconnect":
                return
            chunk = event.get("body", b"")
            size += len(chunk)
            if size > MAX_TOKEN_BODY_BYTES:
                await Response(status_code=413)(scope, receive, send)
                return
            chunks.append(chunk)
            if not event.get("more_body", False):
                break

        body = b"".join(chunks)
        try:
            has_client_id = any(key == "client_id" for key, _ in parse_qsl(body.decode("utf-8"), keep_blank_values=True))
        except UnicodeDecodeError:
            has_client_id = True
        if not has_client_id:
            body += (b"&" if body else b"") + urlencode({"client_id": client_id}).encode("ascii")
            scope = dict(scope)
            scope["headers"] = [
                (key, value) for key, value in scope["headers"] if key.lower() != b"content-length"
            ] + [(b"content-length", str(len(body)).encode("ascii"))]

        delivered = False

        async def replay_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.request", "body": b"", "more_body": False}

        await self.app(scope, replay_receive, send)
