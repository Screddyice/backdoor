"""Hard gates for agent-initiated changes to the live Backdoor path."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.backdoor_live_change_guard import evaluate


LIVE_PLIST = "/Users/screddy/Library/LaunchAgents/com.screddy.backdoor-router.plist"
LIVE_CHECKOUT = "/Users/screddy/projects/SRC/backdoor-service"


@pytest.mark.parametrize(
    "command",
    [
        "launchctl kickstart -k gui/501/com.screddy.backdoor-router",
        "launchctl kill TERM gui/501/com.screddy.backdoor-router",
        "launchctl bootout gui/501/com.screddy.backdoor-router",
        f"launchctl bootstrap gui/501 {LIVE_PLIST}",
        f"/usr/libexec/PlistBuddy -c 'Add :Sockets dict' {LIVE_PLIST}",
        f"cp /tmp/router.plist {LIVE_PLIST}",
        "pkill -f src.proxy.serve",
        "kill $(lsof -tiTCP:8083 -sTCP:LISTEN)",
        "bd-restart --now",
    ],
)
def test_blocks_live_router_mutations_from_shell(command: str) -> None:
    reason = evaluate(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": command},
        }
    )

    assert reason is not None
    assert "user-operated" in reason


@pytest.mark.parametrize(
    "command",
    [
        f"git -C {LIVE_CHECKOUT} checkout --detach origin/main",
        f"git -C {LIVE_CHECKOUT} reset --hard HEAD~1",
        f"cd {LIVE_CHECKOUT} && uv sync --frozen",
        f"{LIVE_CHECKOUT}/.venv/bin/pip install uvicorn",
        "cd ../backdoor-service && git checkout --detach origin/main",
        "cd ../backdoor-service && uv sync --frozen",
    ],
)
def test_blocks_live_checkout_mutations(command: str) -> None:
    assert evaluate({"tool_name": "Bash", "tool_input": {"command": command}})


def test_blocks_nested_codex_exec_payload() -> None:
    payload = {
        "tool_name": "functions.exec",
        "tool_input": (
            "const r = await tools.exec_command({"
            '"cmd":"launchctl bootout gui/501/com.screddy.backdoor-router"});'
        ),
    }

    assert evaluate(payload)


@pytest.mark.parametrize("protected_path", [LIVE_PLIST, f"{LIVE_CHECKOUT}/src/proxy/serve.py"])
def test_blocks_nested_codex_apply_patch_payload(protected_path: str) -> None:
    payload = {
        "tool_name": "functions.exec",
        "tool_input": (
            "const patch = '*** Begin Patch\\n*** Update File: "
            f"{protected_path}"
            "\\n*** End Patch'; await tools.apply_patch(patch);"
        ),
    }

    assert evaluate(payload)


@pytest.mark.parametrize("tool_name", ["Write", "Edit", "MultiEdit", "apply_patch"])
def test_blocks_file_tools_targeting_live_paths(tool_name: str) -> None:
    payload = {
        "tool_name": tool_name,
        "tool_input": {"file_path": LIVE_PLIST, "content": "replacement"},
    }

    assert evaluate(payload)


@pytest.mark.parametrize(
    "command",
    [
        "launchctl print gui/501/com.screddy.backdoor-router",
        f"plutil -p {LIVE_PLIST}",
        f"git -C {LIVE_CHECKOUT} status --short --branch",
        f"git -C {LIVE_CHECKOUT} log -1 --oneline",
        f"git -C {LIVE_CHECKOUT} fetch origin",
        "curl -fsS http://127.0.0.1:8083/health",
        "lsof -nP -iTCP:8084 -sTCP:LISTEN",
    ],
)
def test_allows_read_only_diagnostics(command: str) -> None:
    assert evaluate({"tool_name": "Bash", "tool_input": {"command": command}}) is None


def test_allows_source_repository_edits() -> None:
    payload = {
        "tool_name": "Edit",
        "tool_input": {
            "file_path": "/Users/screddy/projects/SRC/backdoor/src/proxy/serve.py",
            "old_string": "old",
            "new_string": "new",
        },
    }

    assert evaluate(payload) is None


def test_cli_exits_two_and_emits_block_contract() -> None:
    script = Path(__file__).parents[1] / "scripts" / "backdoor_live_change_guard.py"
    payload = {
        "tool_name": "Bash",
        "tool_input": {
            "command": "launchctl kickstart -k gui/501/com.screddy.backdoor-router"
        },
    }

    result = subprocess.run(
        [sys.executable, str(script)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    output = json.loads(result.stdout)
    assert output["decision"] == "block"
    assert "Backdoor" in output["reason"]
