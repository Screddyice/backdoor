"""One gateway's REST API, over async httpx.

Tools fan out across every profile, so this layer must never raise for a
gateway that is simply down. Transport and auth failures come back as
structured state with a reason and a next action; only a programming error
(a missing key in the environment) raises.

The gateway key is read from the environment at call time, never stored on the
instance and never echoed into a response.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from .registry import Profile

STATES = frozenset(
    {"ok", "stopped", "unconfigured", "control_only", "unreachable", "unauthorized"}
)


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
        return {"ok": True, "data": data}

    async def probe(self) -> dict[str, Any]:
        got = await self.request("GET", "/health")
        if got.get("ok"):
            return state(self.profile.name, "ok")
        return got
