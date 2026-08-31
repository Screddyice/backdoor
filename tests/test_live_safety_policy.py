"""Repository gates for the Backdoor control plane."""

from __future__ import annotations

import plistlib
import tomllib
from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_launch_agent_does_not_delegate_router_sockets() -> None:
    with (ROOT / "deploy" / "com.screddy.backdoor-router.plist.example").open("rb") as handle:
        config = plistlib.load(handle)

    assert "Sockets" not in config


def test_qa_assist_cannot_automerge_backdoor() -> None:
    with (ROOT / ".shawns-qa.toml").open("rb") as handle:
        config = tomllib.load(handle)

    assert config["merge"]["enabled"] is False


def test_readme_does_not_tell_agents_to_restart_the_live_router_directly() -> None:
    readme = (ROOT / "README.md").read_text()

    assert "launchctl kickstart -k gui/$(id -u)/com.screddy.backdoor-router" not in readme
    assert "git -C ../backdoor-service checkout --detach origin/main" not in readme

