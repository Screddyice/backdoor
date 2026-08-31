"""Render and count the exact local Qwen prompt without network access."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any, Literal


_COUNT = re.compile(r"Total number of tokens:\s*(\d+)")


@dataclass(frozen=True)
class TokenCount:
    value: int
    source: Literal["llama-tokenize", "utf8-bytes"]
    exact: bool


def _content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return json.dumps(content, ensure_ascii=False, separators=(",", ":"))
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
        elif isinstance(block, dict):
            parts.append(json.dumps(block, ensure_ascii=False, separators=(",", ":")))
        else:
            parts.append(str(block))
    return "\n".join(parts)


def _tool_call_json(call: dict[str, Any]) -> str:
    function = call.get("function") or {}
    arguments = function.get("arguments", {})
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            pass
    body = {
        "name": str(function.get("name", "")),
        "arguments": arguments,
    }
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"))


def render_qwen38_prompt(payload: dict[str, Any]) -> str:
    """Render the Ollama template used by qwen3.8:27b-obliterated."""
    messages = list(payload.get("messages") or [])
    system_parts = [
        _content_text(message.get("content"))
        for message in messages
        if message.get("role") == "system"
    ]
    system = "\n\n".join(part for part in system_parts if part)
    conversation = [
        message for message in messages if message.get("role") != "system"
    ]
    tools = list(payload.get("tools") or [])
    rendered: list[str] = []

    if system or tools:
        rendered.append("<|im_start|>system\n")
        if system:
            rendered.append(system)
        if tools:
            if system:
                rendered.append("\n")
            rendered.append(
                "\n# Tools\n\n"
                "You may call one or more functions to assist with the user query.\n\n"
                "You are provided with function signatures within <tools></tools> XML tags:\n"
                "<tools>\n"
            )
            rendered.extend(
                json.dumps(tool, ensure_ascii=False, separators=(",", ":")) + "\n"
                for tool in tools
            )
            rendered.append(
                "</tools>\n\n"
                "For each function call, return a json object with function name and arguments "
                "within <tool_call></tool_call> XML tags:\n"
                "<tool_call>\n"
                '{"name": <function-name>, "arguments": <args-json-object>}\n'
                "</tool_call>"
            )
        rendered.append("<|im_end|>\n")

    for index, message in enumerate(conversation):
        role = message.get("role")
        last = index == len(conversation) - 1
        if role == "user":
            rendered.append(
                f"<|im_start|>user\n{_content_text(message.get('content'))}<|im_end|>\n"
            )
        elif role == "assistant":
            rendered.append("<|im_start|>assistant\n")
            content = _content_text(message.get("content"))
            tool_calls = list(message.get("tool_calls") or [])
            if content:
                rendered.append(content)
            elif tool_calls:
                rendered.append("<tool_call>\n")
                rendered.extend(_tool_call_json(call) + "\n" for call in tool_calls)
                rendered.append("</tool_call>")
            if not last:
                rendered.append("<|im_end|>\n")
        elif role == "tool":
            rendered.append(
                "<|im_start|>user\n<tool_response>\n"
                f"{_content_text(message.get('content'))}\n"
                "</tool_response><|im_end|>\n"
            )
        else:
            rendered.append(
                f"<|im_start|>{role or 'user'}\n"
                f"{_content_text(message.get('content'))}<|im_end|>\n"
            )
        if role != "assistant" and last:
            rendered.append("<|im_start|>assistant\n")

    return "".join(rendered)


class QwenTokenGate:
    def __init__(
        self,
        executable: str | Path,
        model_path: str | Path,
        *,
        timeout_seconds: float = 12.0,
    ) -> None:
        self.executable = Path(executable).expanduser()
        self.model_path = Path(model_path).expanduser()
        self.timeout_seconds = timeout_seconds

    def _resolved_executable(self) -> Path | None:
        if self.executable.is_file() and os.access(self.executable, os.X_OK):
            return self.executable
        if self.executable.parent == Path("."):
            found = shutil.which(str(self.executable))
            return Path(found) if found else None
        return None

    @staticmethod
    def _byte_bound(rendered: str) -> TokenCount:
        return TokenCount(
            value=len(rendered.encode("utf-8")),
            source="utf8-bytes",
            exact=False,
        )

    def count(self, payload: dict[str, Any]) -> TokenCount:
        rendered = render_qwen38_prompt(payload)
        executable = self._resolved_executable()
        if executable is None or not self.model_path.is_file():
            return self._byte_bound(rendered)
        try:
            result = subprocess.run(
                [
                    str(executable),
                    "-m",
                    str(self.model_path),
                    "--stdin",
                    "--show-count",
                    "--no-bos",
                    "--log-disable",
                ],
                input=rendered,
                text=True,
                capture_output=True,
                timeout=self.timeout_seconds,
                check=True,
            )
            matches = _COUNT.findall(result.stdout)
            if not matches:
                return self._byte_bound(rendered)
            return TokenCount(
                value=int(matches[-1]),
                source="llama-tokenize",
                exact=True,
            )
        except (OSError, subprocess.SubprocessError, ValueError):
            return self._byte_bound(rendered)

    def fits(
        self,
        payload: dict[str, Any],
        hard_limit: int,
    ) -> tuple[bool, TokenCount]:
        counted = self.count(payload)
        return counted.value <= hard_limit, counted
