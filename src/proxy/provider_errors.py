"""Classify provider-edge errors without hiding structured API failures."""

from __future__ import annotations

import json
from collections.abc import Mapping


def is_provider_edge_404(
    status_code: int,
    headers: Mapping[str, str],
    body: bytes,
) -> bool:
    """Return true for an edge-generated 404, not an API-level not-found.

    Both upstreams sit behind Cloudflare, so ``cf-ray`` alone cannot separate a
    transient edge miss from a valid API 404. A valid API error has a JSON body
    with ``error`` or ``detail``; the incident shape was an empty/non-JSON edge
    response that the clients rendered as ``Unknown error``.
    """

    if status_code != 404 or not headers.get("cf-ray"):
        return False

    stripped = body.strip()
    if not stripped:
        return True

    media_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type != "application/json" and not media_type.endswith("+json"):
        return True

    try:
        payload = json.loads(stripped)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return True

    return not (
        isinstance(payload, dict)
        and (payload.get("error") is not None or payload.get("detail") is not None)
    )
