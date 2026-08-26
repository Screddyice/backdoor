"""Token counting via tiktoken (cl100k_base approximation).

The encoder is loaded LAZILY and never fatally. It used to be built at import
time:

    _enc = tiktoken.get_encoding("cl100k_base")

which made every proxy start depend on downloading cl100k_base.tiktoken from
openaipublic.blob.core.windows.net. tiktoken's default cache is a temp dir that
macOS prunes, so the download came back regularly, and a DNS failure while the
Mac was waking or a VPN was settling raised inside `import` — before uvicorn
could even load the app. The process exited "backdoor failed during startup",
launchd relaunched it into the same failure, and because EVERY terminal Claude
session on this machine proxies through :8084, they all sat in retry backoff
showing "Waiting for API response - check your network". Observed 2026-08-26:
two failed starts at 08:10 and the proxy down until 15:10.

Nothing here is worth taking the proxy down for. The only caller is the
failover path in routes.py, which sizes a request to pick a local-model
profile, and that call site is already wrapped in `try/except Exception` with
the note "an unstripped answer beats no answer". Token counts steer a routing
choice; they are not correctness-critical.

So: cache the encoding under ~/.backdoor so it survives temp pruning, build it
on first use rather than at import, and fall back to a character estimate when
it cannot be built at all.
"""

import json
import logging
import os
import pathlib
from typing import Any

import tiktoken

logger = logging.getLogger(__name__)

# Persistent cache. tiktoken defaults to a hash-named directory under the
# system temp dir, which macOS prunes, so the file is re-fetched on a schedule
# nobody chose. Set before the first get_encoding() call for it to take effect.
_CACHE_DIR = pathlib.Path(
    os.environ.get("TIKTOKEN_CACHE_DIR") or (pathlib.Path.home() / ".backdoor" / "tiktoken")
)
try:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("TIKTOKEN_CACHE_DIR", str(_CACHE_DIR))
except OSError:  # unwritable home: fall through to tiktoken's own default
    pass

# Average characters per token for cl100k on English prose. Used only when the
# real encoder is unavailable; a routing threshold tolerates the imprecision,
# a dead proxy does not.
_CHARS_PER_TOKEN = 4

_enc: Any = None
_enc_failed = False


def _encoder() -> Any:
    """Return the cl100k encoder, or None if it cannot be loaded.

    Loaded once. A failure is remembered so a broken network does not make
    every count attempt pay another download timeout.
    """
    global _enc, _enc_failed
    if _enc is not None or _enc_failed:
        return _enc
    try:
        _enc = tiktoken.get_encoding("cl100k_base")
    except Exception as exc:  # network, DNS, corrupt cache
        _enc_failed = True
        logger.warning(
            "tiktoken cl100k_base unavailable (%s); counting with a "
            "~%d-chars-per-token estimate. Routing thresholds get less precise; "
            "nothing else changes.",
            exc,
            _CHARS_PER_TOKEN,
        )
    return _enc


def count_text(text: str) -> int:
    enc = _encoder()
    if enc is None:
        return -(-len(text) // _CHARS_PER_TOKEN)  # ceil division
    return len(enc.encode(text))


def count_messages(
    messages: list[Any],
    system: str | list[dict[str, Any]] | None = None,
    tools: list[Any] | None = None,
) -> int:
    total = 0

    if system:
        if isinstance(system, str):
            total += count_text(system)
        else:
            total += sum(count_text(b.get("text", "")) for b in system if b.get("type") == "text")

    for msg in messages:
        content = msg.content if hasattr(msg, "content") else msg.get("content", "")
        if isinstance(content, str):
            total += count_text(content)
        elif isinstance(content, list):
            for block in content:
                t = block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
                if t == "text":
                    total += count_text(block.get("text", "") if isinstance(block, dict) else block.text)
                elif t in ("tool_use", "tool_result"):
                    total += count_text(json.dumps(block))
        total += 4  # per-message overhead

    if tools:
        total += count_text(json.dumps([t.model_dump() if hasattr(t, "model_dump") else t for t in tools]))

    return total
