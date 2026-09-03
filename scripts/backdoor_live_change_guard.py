#!/usr/bin/env python3
"""Block agent tools from changing the live Backdoor control plane.

The router carries the sessions that would otherwise repair it. Source work and
read-only diagnostics remain available; live deployment and launchd operations
belong to the user in an independent Terminal session.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable


LIVE_HOME = Path("/Users/screddy")
LIVE_PLIST = LIVE_HOME / "Library/LaunchAgents/com.screddy.backdoor-router.plist"
LIVE_CHECKOUT = LIVE_HOME / "projects/SRC/backdoor-service"
ROUTER_LABEL = "com.screddy.backdoor-router"

FILE_MUTATION_TOOLS = {"write", "edit", "multiedit", "apply_patch"}
LAUNCHD_MUTATIONS = re.compile(
    r"\blaunchctl\s+(?:kickstart|kill|bootout|bootstrap|load|unload|remove|start|stop)\b",
    re.IGNORECASE,
)
PLIST_MUTATIONS = re.compile(
    r"\b(?:PlistBuddy|plutil)\b.*(?:\bAdd\b|\bSet\b|\bDelete\b|\bMerge\b|"
    r"\s-(?:replace|insert|remove)\b)",
    re.IGNORECASE | re.DOTALL,
)
SHELL_FILE_MUTATIONS = re.compile(
    r"(?:\b(?:cp|mv|rm|install|chmod|chflags|tee)\b|\bsed\s+-[^\n;]*i|"
    r"\bperl\s+-[^\n;]*i|(?:^|[;&|]\s*)python(?:3(?:\.\d+)?)?\b|>{1,2})",
    re.IGNORECASE,
)
LIVE_GIT_MUTATIONS = re.compile(
    r"\bgit\b[^\n;]*(?:\b-C\b[^\n;]*)?\b"
    r"(?:checkout|switch|reset|pull|merge|rebase|cherry-pick|restore|clean|commit)\b",
    re.IGNORECASE,
)
LIVE_ENV_MUTATIONS = re.compile(
    r"(?:\buv\s+sync\b|\bpip(?:3)?\s+install\b|/\.venv/bin/pip\s+install\b)",
    re.IGNORECASE,
)
PROCESS_MUTATIONS = re.compile(
    r"(?:\b(?:pkill|killall)\b[^\n;]*src\.proxy\.serve|"
    r"\bkill\b[^\n;]*(?:lsof[^\n;]*(?:8083|8084)|src\.proxy\.serve))",
    re.IGNORECASE,
)

BLOCK_REASON = (
    "Backdoor's live control plane is user-operated. Agents may inspect it and edit source, "
    "but may not change the live launch agent, deployed checkout, dependencies, or router "
    "process. Use an independent Terminal session and keep a direct Claude or Codex rescue "
    "path open before any live operation."
)


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def _tool_name(payload: dict[str, Any]) -> str:
    return str(payload.get("tool_name") or payload.get("tool") or "").lower()


def _tool_input(payload: dict[str, Any]) -> Any:
    return payload.get("tool_input", payload.get("input", {}))


def _contains_path(text: str, path: Path) -> bool:
    absolute = str(path)
    home_form = absolute.replace(str(LIVE_HOME), "$HOME", 1)
    tilde_form = absolute.replace(str(LIVE_HOME), "~", 1)
    return any(candidate in text for candidate in (absolute, home_form, tilde_form))


def _file_target(tool_input: Any) -> str:
    if not isinstance(tool_input, dict):
        return ""
    return str(
        tool_input.get("file_path")
        or tool_input.get("file")
        or tool_input.get("path")
        or ""
    )


def _is_protected_file(path_text: str) -> bool:
    if not path_text:
        return False
    expanded = Path(path_text.replace("$HOME", str(Path.home()))).expanduser()
    try:
        resolved = expanded.resolve(strict=False)
    except OSError:
        resolved = expanded
    return resolved == LIVE_PLIST or resolved == LIVE_CHECKOUT or LIVE_CHECKOUT in resolved.parents


def evaluate(payload: dict[str, Any]) -> str | None:
    """Return the block reason for a dangerous tool call, otherwise ``None``."""

    tool_name = _tool_name(payload)
    tool_input = _tool_input(payload)
    target = _file_target(tool_input)

    if any(tool_name.endswith(name) for name in FILE_MUTATION_TOOLS):
        if _is_protected_file(target):
            return BLOCK_REASON
        if tool_name.endswith("apply_patch"):
            patch_text = "\n".join(_strings(tool_input))
            if _contains_path(patch_text, LIVE_PLIST) or _contains_path(
                patch_text, LIVE_CHECKOUT
            ):
                return BLOCK_REASON

    text = "\n".join(_strings(tool_input))
    if not text:
        return None

    if "apply_patch" in text and (
        _contains_path(text, LIVE_PLIST) or _contains_path(text, LIVE_CHECKOUT)
    ):
        return BLOCK_REASON

    targets_router = ROUTER_LABEL in text or _contains_path(text, LIVE_PLIST)
    targets_checkout = _contains_path(text, LIVE_CHECKOUT) or bool(
        re.search(r"(?:^|[/\s])backdoor-service(?:[/\s]|$)", text)
    )

    if targets_router and LAUNCHD_MUTATIONS.search(text):
        return BLOCK_REASON
    if _contains_path(text, LIVE_PLIST) and (
        PLIST_MUTATIONS.search(text) or SHELL_FILE_MUTATIONS.search(text)
    ):
        return BLOCK_REASON
    if targets_checkout and (
        LIVE_GIT_MUTATIONS.search(text)
        or LIVE_ENV_MUTATIONS.search(text)
        or SHELL_FILE_MUTATIONS.search(text)
    ):
        return BLOCK_REASON
    if re.search(r"(?:^|[/\s])bd-restart(?:\s|$)", text):
        return BLOCK_REASON
    if PROCESS_MUTATIONS.search(text):
        return BLOCK_REASON
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return 0

    if not isinstance(payload, dict):
        return 0
    reason = evaluate(payload)
    if reason is None:
        return 0

    print(json.dumps({"decision": "block", "reason": reason}))
    print(f"BLOCKED: {reason}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
