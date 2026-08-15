"""One gateway's REST API, over async httpx.

Tools fan out across every profile, so this layer must never raise for a
gateway that is simply down. Transport and auth failures come back as
structured state with a reason and a next action; only a programming error
(a missing key in the environment) raises.

The gateway key is read from the environment at call time, never stored on the
instance and never echoed into a response.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from .registry import Profile

logger = logging.getLogger(__name__)

STATES = frozenset(
    {"ok", "stopped", "unconfigured", "control_only", "unreachable", "unauthorized"}
)

#: Stands in for the gateway key wherever a gateway echoed it back to us. Fixed
#: text, so it discloses neither the key's length nor where in the body it sat.
REDACTED = "[redacted: gateway key]"


def _redact(value: Any, key: str) -> Any:
    """Return *value* with every occurrence of *key* replaced by REDACTED.

    Recurses through the containers json.loads can produce, so a key echoed in a
    nested field is caught as surely as one at the top level, and rebuilds the
    structure rather than stringifying it: a caller still receives real JSON
    types. Non-str leaves cannot contain the key and are returned untouched.
    """
    if not key:
        return value
    if isinstance(value, str):
        return value.replace(key, REDACTED)
    if isinstance(value, dict):
        return {_redact(k, key): _redact(v, key) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(item, key) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact(item, key) for item in value)
    return value


class MissingKey(RuntimeError):
    """The env var named by the profile's key_env is unset or empty."""


def state(
    profile: str, state: str, *, reason: str | None = None, next: str | None = None
) -> dict[str, Any]:
    """The one structured-state shape every tool returns for a non-ok profile."""
    assert state in STATES, f"unknown state {state!r}"
    return {"profile": profile, "state": state, "reason": reason, "next": next}


class GatewayClient:
    def __init__(self, profile: Profile, *, timeout: float = 10.0) -> None:
        self.profile = profile
        self.timeout = timeout
        self._transport: httpx.BaseTransport | None = None

    def _key(self) -> str:
        value = os.environ.get(self.profile.key_env or "", "")
        if not value:
            raise MissingKey(
                f"{self.profile.key_env} is unset; the bridge cannot authenticate "
                f"to profile {self.profile.name!r}"
            )
        return value

    async def request(
        self, method: str, path: str, json: dict | None = None
    ) -> dict[str, Any]:
        p = self.profile
        if not p.reachable:
            return state(
                p.name,
                "unconfigured",
                reason="no API server port or key is registered for this profile",
                next="add port and key_env to the registry once the gateway is configured",
            )

        key = self._key()
        url = f"http://127.0.0.1:{p.port}{path}"
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout, transport=self._transport
            ) as client:
                resp = await client.request(
                    method, url, json=json,
                    headers={"Authorization": f"Bearer {key}"},
                )
        except httpx.ConnectError:
            return state(
                p.name, "stopped",
                reason=f"nothing is listening on port {p.port}",
                next=(f"start {p.unit}" if p.unit else
                      "no systemd unit is registered; install the gateway first"),
            )
        except httpx.TimeoutException:
            return state(
                p.name, "unreachable",
                reason=f"no response within {self.timeout:g}s",
                next="check whether the gateway is wedged",
            )
        except httpx.HTTPError as exc:
            return state(
                p.name, "unreachable",
                reason=f"transport error: {type(exc).__name__}",
                next="check the gateway process and the registered port",
            )
        except Exception as exc:
            # Anything outside httpx's own error hierarchy is assumed to carry
            # key material and is never described to the caller.
            #
            # The live case: httpx encodes header values as ASCII, so a gateway
            # key holding a non-ASCII character raises UnicodeEncodeError while
            # building "Bearer <key>". Its message names the offending character
            # and its offset in that header -- a character of the key, and where
            # in the key it sits. UnicodeEncodeError is not an httpx.HTTPError,
            # so without this clause it escapes request(), escapes tools._call()
            # (which catches MissingKey only) and reaches the caller as tool
            # error text.
            #
            # Catching broadly, and returning one fixed reason, is the point.
            # Naming UnicodeEncodeError alone would leave the next non-HTTPError
            # type as the same disclosure, and deriving the text from type(exc)
            # would let a caller tell which internal failure occurred. Only the
            # type is logged: the message is the part that holds key material.
            logger.error(
                "request to profile %s failed with %s; "
                "check the value of %s (message withheld: it can echo the key)",
                p.name, type(exc).__name__, p.key_env,
            )
            return state(
                p.name, "unreachable",
                reason="the bridge could not issue the request to this gateway",
                next=f"check {p.key_env} in the bridge environment, then the bridge logs",
            )

        if resp.status_code in (401, 403):
            # Deliberately does not include the response body: a gateway is
            # free to echo the key it was sent, and that must not reach a caller.
            return state(
                p.name, "unauthorized",
                reason=f"gateway rejected the key from {p.key_env} ({resp.status_code})",
                next=f"rotate {p.key_env} in the bridge env and the profile .env together",
            )
        if resp.status_code >= 400:
            return state(
                p.name, "unreachable",
                reason=f"gateway returned HTTP {resp.status_code}",
                next="check the gateway logs",
            )

        try:
            data = resp.json()
        except ValueError:
            data = {"text": resp.text}
        # A 2xx body is gateway-supplied content, and the reasoning applied to
        # 401/403 above holds here too: a gateway is free to echo the key it was
        # sent, on any status code. Arbitrary content cannot be filtered, but the
        # one string that must not pass is known exactly, so redact by value.
        # This sits on the single return that carries a body, so the JSON branch
        # and the plain-text fallback are both covered by construction.
        return _redact({"ok": True, "data": data}, key)

    async def probe(self) -> dict[str, Any]:
        got = await self.request("GET", "/health")
        if got.get("ok"):
            return state(self.profile.name, "ok")
        return got
