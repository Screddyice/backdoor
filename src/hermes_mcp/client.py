"""One gateway's REST API, over async httpx.

Tools fan out across every profile, so this layer must never raise for a
gateway that is simply down. Transport and auth failures come back as
structured state with a reason and a next action; only a programming error
(a missing key in the environment) raises.

That contract covers the whole of request(), not only the HTTP exchange:
handling a response can fail too, and a failure there is still one gateway's
problem rather than every profile's. request() is therefore total by
construction, wrapping the attempt instead of guarding chosen spots inside it.

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
    {
        "ok",
        "stopped",
        "unconfigured",
        "control_only",
        "delegate_only",
        "unreachable",
        "unauthorized",
    }
)

#: Stands in for the gateway key wherever a gateway echoed it back to us. Fixed
#: text, so it discloses neither the key's length nor where in the body it sat.
REDACTED = "[redacted: gateway key]"


def _redact(value: Any, key: str) -> Any:
    """Return *value* with every occurrence of *key* replaced by REDACTED.

    Recurses through the containers json.loads can produce, so a key echoed in a
    nested field is caught as surely as one at the top level, and in a dict key
    as surely as in a value. Containers are rebuilt rather than stringified, so a
    caller still receives real JSON types.

    A non-str leaf is checked against its own string form, not skipped. An
    all-digit key is a legal key -- 32 digits clears the bridge's strength guard
    -- and a gateway echoing it as a JSON *number* would otherwise sail through
    untouched. Such a leaf comes back as the placeholder string, changing its
    JSON type: a leaked key is worse than a number arriving as a string.
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
    return REDACTED if key in str(value) else value


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

    def _opaque_failure(self, exc: BaseException) -> dict[str, Any]:
        """The one state returned for any internal failure, whatever it was.

        Fixed text on purpose. Deriving it from type(exc) would let a caller
        probe for which internal failure occurred, and anything derived from
        str(exc) can carry key material -- a UnicodeEncodeError from encoding
        "Bearer <key>" names a character of the key and its offset. Only the
        type is logged; the message never is.
        """
        p = self.profile
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

    async def request(
        self, method: str, path: str, json: dict | None = None
    ) -> dict[str, Any]:
        """Total, except for MissingKey. Never raises for anything a gateway did.

        Tools fan out across every profile, so this contract is what stops one
        gateway from failing a whole listing. It has to hold for the *whole*
        call, not just the httpx exchange: response handling raises too. A body
        nested deeply enough overflows the stack in json.loads or in _redact's
        recursion, and RecursionError is not an httpx error, so before this
        wrapper existed it escaped here, escaped tools._call() -- which catches
        MissingKey only -- and surfaced to the MCP caller.

        _issue() keeps the httpx-specific handling, which distinguishes a
        stopped gateway from a wedged one and is worth reporting precisely.
        This wrapper is the backstop for everything else, on every path, and
        answers with one indistinguishable state.

        MissingKey is re-raised deliberately: it is a bridge misconfiguration,
        not a gateway failure, and tools._call() turns it into its own
        'unconfigured' state naming the env var an operator has to set.
        """
        try:
            return await self._issue(method, path, json)
        except MissingKey:
            raise
        except Exception as exc:
            return self._opaque_failure(exc)

    async def _issue(
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
        # No catch-all here. Anything outside httpx's hierarchy -- the
        # UnicodeEncodeError from ASCII-encoding a non-ASCII key into
        # "Bearer <key>" being the live case -- is caught by request(), which
        # wraps this whole method rather than just the exchange above. One
        # catch-all, one reason, so no path can answer differently from another.

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
