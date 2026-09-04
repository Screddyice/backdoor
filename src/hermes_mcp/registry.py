"""Profile registry for the Hermes MCP bridge.

A Hermes host runs one gateway process per profile, each with its own
HERMES_HOME, .env and API server port. There is no endpoint fronting them all,
so the bridge needs a map, and this file is the only place that map exists.

Validation is strict on purpose. The tier decides whether a profile may be
conversed with, so an unrecognised tier must fail at load rather than default
to something permissive. Duplicate ports must fail too: two profiles claiming
one port means one is unreachable, or is answering for the other.

Deployment values never live in this repo. The file is read from
HERMES_MCP_REGISTRY, or ~/.config/hermes-mcp/registry.toml.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

TIERS = frozenset({"full", "control_only", "delegate_only", "unconfigured"})


class RegistryError(ValueError):
    """The registry is missing, unparseable, or internally inconsistent."""


@dataclass(frozen=True)
class Profile:
    name: str
    tier: str
    port: int | None = None
    key_env: str | None = None
    unit: str | None = None
    home: str | None = None

    @property
    def reachable(self) -> bool:
        """True when the profile has an API server the bridge can call."""
        return self.port is not None and self.key_env is not None


def registry_path() -> Path:
    override = os.environ.get("HERMES_MCP_REGISTRY")
    if override:
        return Path(override)
    return Path.home() / ".config" / "hermes-mcp" / "registry.toml"


def load_registry(path: str | Path | None = None) -> dict[str, Profile]:
    target = Path(path) if path is not None else registry_path()
    try:
        raw = tomllib.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RegistryError(f"registry not found at {target}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise RegistryError(f"registry at {target} is not valid TOML: {exc}") from exc

    entries = raw.get("profiles") or {}
    if not entries:
        raise RegistryError(f"registry at {target} declares no [profiles.*] entries")

    profiles: dict[str, Profile] = {}
    ports: dict[int, str] = {}
    for name, body in entries.items():
        tier = body.get("tier")
        if tier not in TIERS:
            raise RegistryError(
                f"profile {name!r} has tier {tier!r}; expected one of {sorted(TIERS)}"
            )

        port = body.get("port")
        key_env = body.get("key_env")
        if tier in {"full", "control_only", "delegate_only"}:
            if port is None:
                raise RegistryError(f"profile {name!r} is {tier} but declares no port")
            if key_env is None:
                raise RegistryError(f"profile {name!r} is {tier} but declares no key_env")

        if port is not None:
            clash = ports.get(port)
            if clash is not None:
                raise RegistryError(
                    f"port {port} is claimed by both {clash!r} and {name!r}; "
                    "concurrently running profiles must not share a port"
                )
            ports[port] = name

        profiles[name] = Profile(
            name=name,
            tier=tier,
            port=port,
            key_env=key_env,
            unit=body.get("unit"),
            home=body.get("home"),
        )

    return profiles
