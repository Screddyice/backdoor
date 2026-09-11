"""Fail-closed token accounting for local Qwen provider payloads."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import subprocess
from typing import Any


_TOKEN_COUNT = re.compile(r"\btokens?\s*[:=]\s*(\d+)\b", re.IGNORECASE)
_TOKENIZED_COUNT = re.compile(r"\btokenized\s+(\d+)\s+tokens?\b", re.IGNORECASE)


class ContextLimitError(ValueError):
    """The local provider payload has no proof that it fits its input window."""


@dataclass(frozen=True)
class TokenCount:
    tokens: int
    exact: bool
    method: str


class QwenTokenGate:
    """Count the exact OpenAI-compatible provider JSON or use a safe upper bound."""

    def __init__(self, *, executable: str, model_path: str) -> None:
        self.executable = executable
        self.model_path = model_path

    def render(self, payload: dict[str, Any]) -> str:
        """Preserve the provider's insertion and list order, including messages and tools."""
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    def count(self, payload: dict[str, Any]) -> TokenCount:
        rendered = self.render(payload)
        exact = self._exact_count(rendered)
        if exact is not None:
            return TokenCount(exact, True, "llama-tokenize")
        return TokenCount(len(rendered.encode("utf-8")), False, "utf8_bytes")

    def require_fit(self, payload: dict[str, Any], hard_tokens: int) -> TokenCount:
        result = self.count(payload)
        if result.tokens > hard_tokens:
            raise ContextLimitError(
                f"local Qwen payload needs {result.tokens} tokens, above the {hard_tokens}-token limit"
            )
        return result

    def _exact_count(self, rendered: str) -> int | None:
        executable = Path(self.executable).expanduser()
        model = Path(self.model_path).expanduser()
        if not executable.is_file() or not model.is_file():
            return None
        try:
            completed = subprocess.run(
                [
                    str(executable),
                    "-m",
                    str(model),
                    "--prompt",
                    rendered,
                    "--show-count",
                    "--log-disable",
                ],
                capture_output=True,
                check=False,
                text=True,
                timeout=12,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if completed.returncode != 0:
            return None
        for pattern in (_TOKEN_COUNT, _TOKENIZED_COUNT):
            match = pattern.search(completed.stdout)
            if match is not None:
                return int(match.group(1))
        lines = completed.stdout.splitlines()
        if len(lines) == 1 and lines[0].strip().isdigit():
            return int(lines[0].strip())
        return None
