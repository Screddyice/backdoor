"""FastAPI route handlers."""

import asyncio
import json
import logging
import uuid
from typing import AsyncIterator

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from starlette.background import BackgroundTask

from .config import (
    MODEL_ROUTES, Settings, get_settings, load_profile_settings, pick_failover_profile,
    resolve_model_route,
)
from .bare import make_bare, parse_keep
from .external_context import prepare_external_context
from .failover import FAILOVER_STATUSES, FailoverBreaker
from . import compute_lease, mlx_admin, ollama_admin
from .models import MessagesRequest, TokenCountRequest, MessagesResponse, TokenCountResponse, Usage
from .client import ProviderClient, ProviderError
from .tokens import count_messages
from .translate import build_nim_payload, nim_response_to_anthropic, start_stream_events, stream_openai_to_anthropic
from .optimizations import (
    is_quota_probe, is_title_generation, is_suggestion_mode,
    is_prefix_detection, extract_prefix,
    is_filepath_extraction, extract_filepaths,
)

logger = logging.getLogger(__name__)
router = APIRouter()

_client: ProviderClient | None = None


def set_provider_client(c: ProviderClient):
    global _client
    _client = c


def get_provider_client() -> ProviderClient:
    if _client is None:
        raise RuntimeError("Provider client not initialised")
    return _client


# ── Hybrid-mode plumbing ─────────────────────────────────────────────────────
# Lazily-built clients for the local profiles MODEL_ROUTES points at, plus a
# raw passthrough client for the real Anthropic API.

_profile_clients: dict[str, ProviderClient] = {}
_upstream_client: httpx.AsyncClient | None = None


def _get_profile_client(profile: str, psettings: Settings) -> ProviderClient:
    if profile not in _profile_clients:
        _profile_clients[profile] = ProviderClient(psettings)
    return _profile_clients[profile]


def _new_upstream_client(settings: Settings) -> httpx.AsyncClient:
    # connect=30: connect covers DNS + TCP + TLS over a path we do not control.
    # 2026-08-20 a VPN detour (~264ms/hop, high jitter) pushed setup past the
    # old 10s limit 572 times in one evening, each one a user-visible retry
    # banner. Claude Code talking to Anthropic directly just waits out a slow
    # connect, so the router must extend the same tolerance.
    return httpx.AsyncClient(
        base_url=settings.anthropic_upstream,
        timeout=httpx.Timeout(connect=30.0, read=600.0, write=60.0, pool=5.0),
    )


def _get_upstream(settings: Settings) -> httpx.AsyncClient:
    global _upstream_client
    if _upstream_client is None:
        _upstream_client = _new_upstream_client(settings)
    return _upstream_client


def _rotate_upstream(
    settings: Settings,
    poisoned: httpx.AsyncClient,
) -> tuple[httpx.AsyncClient, bool]:
    """Replace one exhausted shared pool without racing concurrent requests."""
    global _upstream_client
    if _upstream_client is poisoned:
        _upstream_client = _new_upstream_client(settings)
        return _upstream_client, True
    return _get_upstream(settings), False


# Hop-by-hop / transport headers we must not forward. Note accept-encoding IS
# forwarded (and content-encoding kept on the response): we stream the raw
# upstream bytes, so the encoding headers stay truthful and the client's own
# decompression applies. If the client sent none, we pin identity — otherwise
# httpx injects its own accept-encoding and the client gets unannounced gzip.
_HOP_HEADERS = {
    "host", "content-length", "connection", "keep-alive", "transfer-encoding",
    "te", "upgrade", "proxy-authorization", "proxy-authenticate",
}
_SKIP_RESP_HEADERS = {"content-length", "transfer-encoding", "connection"}
# Additional headers to drop when we hand the client an ALREADY-DECODED body.
# httpx's .aread()/.aiter_bytes() undo content-encoding, so keeping the upstream
# content-encoding would make a gzip-negotiating client try to gunzip plaintext
# and raise "Error -3 while decompressing data: incorrect header check".
# content-length is already stripped above; Starlette recomputes it.
_DECODED_SKIP_HEADERS = _SKIP_RESP_HEADERS | {"content-encoding"}


def _decoded_relay_headers(uresp: httpx.Response) -> dict[str, str]:
    """Response headers safe to forward alongside an already-decoded body."""
    return {k: v for k, v in uresp.headers.items() if k.lower() not in _DECODED_SKIP_HEADERS}


async def _upstream_send(request: Request, body: bytes, settings: Settings) -> httpx.Response:
    """Open a byte-faithful streaming request against the real Anthropic API
    (auth headers and all) and return it at the headers-received stage."""
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_HEADERS}
    headers.setdefault("accept-encoding", "identity")
    url = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    upstream = _get_upstream(settings)
    for attempt in range(2):
        ureq = upstream.build_request(request.method, url, content=body, headers=headers)
        try:
            return await upstream.send(ureq, stream=True)
        except httpx.TransportError as exc:
            if attempt:
                raise
            if isinstance(exc, httpx.PoolTimeout):
                poisoned = upstream
                upstream, rotated = _rotate_upstream(settings, poisoned)
                if rotated:
                    logger.warning(
                        "Anthropic connection pool exhausted; rotated pool and retrying once"
                    )
                    await poisoned.aclose()
            else:
                logger.warning(
                    "Anthropic transport failed (%s); retrying once before failover",
                    type(exc).__name__,
                )
    raise RuntimeError("unreachable")


async def _relay_body(uresp: httpx.Response, settings: Settings) -> AsyncIterator[bytes]:
    """Stream the upstream body through, counting a mid-stream death.

    `_try_upstream` only guards up to the headers-received stage. Once headers
    are on the wire the response is committed: FastAPI has sent 200 and the
    client is reading a body, so there is no longer anything to substitute — the
    local model cannot take over a turn the client is already halfway through.
    That part is unavoidable, and the connection still dies.

    What was avoidable is that it died SILENTLY. `aiter_raw()` went straight
    into StreamingResponse, so a transport error inside it escaped to uvicorn,
    whose only answer mid-response is to tear the connection down — rendered by
    Claude Code as `The response stopped arriving. The response above may be
    incomplete.` — and it never reached :meth:`FailoverBreaker.record_failure`.

    That is the same two-part bug `_guarded_passthrough` was written for on
    2026-08-24, one layer further downstream, and it cost a 2026-08-26 23:17
    incident its entire diagnostic trail: the router logged NOTHING for the
    failed turn, because the only code that logs and counts had already
    returned successfully three minutes earlier.

    Counting it matters even though this request is lost. A truncated stream is
    a strong signal that the next request is about to fail the same way, and the
    client retries within seconds; feeding it to the breaker is what lets that
    retry be served locally instead of truncating again. The breaker still
    decides on its own terms — `record_failure` runs the connectivity probe, so
    a stream Anthropic dropped while this host is online relays the failure
    rather than claiming the GPU.

    A client disconnect must NOT be counted: that is `asyncio.CancelledError` /
    `GeneratorExit`, neither of which is an `httpx.TransportError`, so both pass
    through untouched.
    """
    try:
        async for chunk in uresp.aiter_raw():
            yield chunk
    except httpx.TransportError as exc:
        logger.warning(
            "upstream stream died mid-response after %d byte(s) (%s): %s — "
            "headers were already sent, so this turn cannot be failed over",
            uresp.num_bytes_downloaded, type(exc).__name__, exc,
        )
        get_breaker(settings).record_failure(type(exc).__name__)
        raise


def _relay_upstream(uresp: httpx.Response, settings: Settings) -> StreamingResponse:
    resp_headers = {k: v for k, v in uresp.headers.items() if k.lower() not in _SKIP_RESP_HEADERS}
    return StreamingResponse(
        _relay_body(uresp, settings),
        status_code=uresp.status_code,
        headers=resp_headers,
        background=BackgroundTask(uresp.aclose),
    )


async def _guarded_passthrough(request: Request, body: bytes, settings: Settings):
    """Forward a request byte-faithfully to the real Anthropic API and stream
    the response back untouched (auth headers, SSE framing and all), catching
    and counting a transport failure instead of raising it.

    Returns the relayed response, or None when upstream could not be reached
    and the caller must decide what to serve instead.

    Guards rather than raises because the plain passthrough this replaces was
    reachable from three handlers that had no `except` around it. A
    ConnectTimeout there escaped as an unhandled ASGI exception, and uvicorn's
    only answer to that mid-response is to tear the client connection down —
    which Claude Code renders as `Connection dropped (ECONNRESET)`, the
    2026-08-24 incident. The client then retried into the same unguarded
    handler, so the banner counted to 10 while the router logged tracebacks
    instead of the one line that says what failed.

    The second half of the bug was quieter and worse: those failures never
    reached :meth:`FailoverBreaker.record_failure`. /v1/messages/count_tokens
    fires on nearly every turn, so during a real outage most of the evidence
    that Anthropic was unreachable was raised and discarded — the breaker
    counted a fraction of the failures and took correspondingly longer to open,
    or never reached the threshold inside its window at all. Recording here is
    evidence-gathering only; the breaker still decides on its own terms, and
    :func:`internet_reachable` still has the final say on opening.

    Deliberately does NOT call record_success. Closing the breaker obliges the
    caller to release the local tiers it claimed (see `_try_upstream`), and only
    the /v1/messages path knows how to do that; a bare success here would close
    the breaker and leave a qwen tier resident with nothing to unload it.
    """
    try:
        uresp = await _upstream_send(request, body, settings)
    except httpx.TransportError as e:
        logger.warning("upstream transport failure (%s): %s", type(e).__name__, e)
        get_breaker(settings).record_failure(type(e).__name__)
        return None
    return _relay_upstream(uresp, settings)


# ── Cloud→local failover ─────────────────────────────────────────────────────
# One breaker for the process (hybrid mode has a single upstream). Lazily
# built from settings so env overrides apply.

_breaker: FailoverBreaker | None = None


def get_breaker(settings: Settings) -> FailoverBreaker:
    global _breaker
    if _breaker is None:
        _breaker = FailoverBreaker(
            threshold=settings.failover_threshold,
            window=settings.failover_window_seconds,
            probe_interval=settings.failover_probe_seconds,
            min_outage=settings.failover_min_outage_seconds,
            notify_cooldown=settings.failover_notify_cooldown_seconds,
        )
    return _breaker


async def failover_recovery_loop(
    settings: Settings, targets=None, sleep=asyncio.sleep
) -> None:
    """Close an open breaker once this host is back online, without a rider.

    The breaker's own half-open path needs an upstream-bound request to carry
    it, and an outage is exactly what stops those arriving: sessions served by a
    local tier generate far less traffic, and a user who walks away from a
    degraded session generates none. On 2026-09-02 that left the breaker open
    for 77 minutes on a network that had recovered — every routed session
    silently on qwen — until unrelated traffic finally closed it. This loop is
    the ticker that outage cannot switch off.

    `targets` is a list of `(breaker, release)` pairs, `release` being an async
    callable taking the breaker. It defaults to both breakers this router owns:
    the Claude one here and the Codex one in codex_routes, each with its own
    tier-release path. Only a breaker that required an offline host to open can
    actually be closed this way — `maybe_recover` refuses for a service-level
    breaker — but passing it costs nothing and keeps the wiring honest if that
    configuration changes.

    Cheap by construction: it does nothing at all while the breakers are closed,
    which is essentially always. The probe itself is blocking socket work, so it
    goes to a worker thread rather than stalling the event loop the router is
    serving requests on.
    """
    if targets is None:
        # Imported here rather than at module scope: codex_routes is a peer that
        # pulls in the whole Codex relay, and this is the only thing here that
        # needs it.
        from .codex_routes import _release_claims as _release_codex_claims
        from .codex_routes import get_codex_breaker

        targets = [
            (get_breaker(settings), lambda br: _release_claims(br, settings)),
            (get_codex_breaker(settings), _release_codex_claims),
        ]

    # Each breaker rations its own probe by its own interval, so the tick only
    # has to be fast enough for the shorter of the two.
    interval = max(
        1.0,
        min(settings.failover_probe_seconds, settings.codex_failover_probe_seconds),
    )
    # Logged once, at INFO, so a deploy can be confirmed from the log rather than
    # by waiting for an outage: this line and the min-outage value it prints are
    # the cheapest proof that the new failover policy is the one running.
    logger.info(
        "failover recovery ticker armed — every %.0fs, min-outage %.0fs/%.0fs "
        "(claude/codex), notify cooldown %.0fs",
        interval,
        settings.failover_min_outage_seconds,
        settings.codex_failover_min_outage_seconds,
        settings.failover_notify_cooldown_seconds,
    )
    while True:
        await sleep(interval)
        for breaker, release in targets:
            if not breaker.open:
                continue
            try:
                closed = await asyncio.to_thread(breaker.maybe_recover)
            except Exception:  # a failed probe must never kill the ticker
                logger.exception("failover recovery probe failed")
                continue
            if closed:
                try:
                    await release(breaker)
                except Exception:
                    logger.exception("failover recovery could not release tiers")


# Failover responses still being generated, and the tiers whose release is
# waiting on them. See _release_claims for why the two cannot be independent.
_failover_inflight: int = 0
_deferred_unloads: set[tuple[str, str]] = set()


async def _release_claims(br: FailoverBreaker, settings: Settings) -> None:
    """Hand back the local tiers a closing breaker no longer needs.

    Releasing on close is precise where Ollama's global 5m idle timer is not,
    and the timer is refreshed by every request an outage generates, so a busy
    outage releases LATER than a quiet one. That is why this exists.

    But "the breaker closed" is not the same as "nothing is using the tier". The
    breaker closes on the first upstream SUCCESS, and that success is a
    different, newer request than the failover streams still running — a local
    tier prefilling a large session emits nothing for minutes, so a stream
    dispatched during the outage is routinely still open when the outage ends.
    Unloading underneath it evicts the model that stream is mid-generation on.

    Observed 2026-08-26: a failover stream opened at 23:10:34 was still running
    when the breaker closed at 23:14:17 and unloaded `qwen3.5:4b-256k` in the
    same 62ms window; the stream then produced nothing until it died on the
    600-second read timeout at 23:20:38.

    So the unload waits for the last in-flight failover response, and is dropped
    entirely if a FRESH outage re-opened the breaker while it waited — that tier
    is claimed again and releasing it would evict a model now in use.
    """
    claims = br.drain_claims()
    if not claims:
        return
    if _failover_inflight:
        # Deferred, not backgrounded: a detached task cannot see a later
        # re-open, and would unload a tier the next outage had re-claimed.
        _deferred_unloads.update(claims)
        logger.info(
            "breaker closed with %d failover response(s) still streaming — "
            "deferring release of %s",
            _failover_inflight, ", ".join(sorted(m for _, m in claims)),
        )
        return
    for base_url, model in claims:
        await ollama_admin.unload(base_url, model)


def _failover_stream_started() -> None:
    global _failover_inflight
    _failover_inflight += 1


def _failover_stream_ended() -> set[tuple[str, str]]:
    """Drop this response from the in-flight count; return any tiers now due.

    Split from the unloading itself, and deliberately free of `await`, because
    the only caller is a `finally` inside an async generator: entered on normal
    completion, on error, and on the `aclose()` a client disconnect triggers.
    Decrementing before any suspension point means a cancellation landing on the
    release cannot leave the count permanently elevated — and an elevated count
    would defer every future unload forever, which is a worse failure than the
    race this fixes.
    """
    global _failover_inflight, _deferred_unloads
    _failover_inflight = max(0, _failover_inflight - 1)
    if _failover_inflight or not _deferred_unloads:
        return set()
    claims, _deferred_unloads = _deferred_unloads, set()
    return claims


async def _release_deferred(
    claims: set[tuple[str, str]], settings: Settings
) -> None:
    """Unload tiers whose release waited for the last failover response."""
    if get_breaker(settings).open:
        # Re-opened while we waited: the tier is claimed again, and the new
        # outage's own close is what should release it.
        logger.info("failover re-opened while releasing — leaving tiers resident")
        return
    for base_url, model in claims:
        await ollama_admin.unload(base_url, model)


async def _tracked_failover_stream(
    inner: AsyncIterator[str], settings: Settings
) -> AsyncIterator[str]:
    """Hold a failover response's tier claim open for as long as it generates."""
    _failover_stream_started()
    try:
        async for event in inner:
            yield event
    finally:
        due = _failover_stream_ended()
        if due:
            try:
                await _release_deferred(due, settings)
            except Exception:  # release is housekeeping; never mask the response
                logger.exception("deferred tier release failed")


async def _try_upstream(request: Request, body: bytes, settings: Settings):
    """Attempt the real Anthropic API for a /v1/messages passthrough.

    Returns a response to relay, or None when the caller should serve the
    request from the local failover profile instead. Mid-stream failures after
    headers are NOT intercepted — the client's retry then hits the breaker."""
    br = get_breaker(settings)
    if not br.allow_upstream():
        return None
    try:
        uresp = await _upstream_send(request, body, settings)
    except httpx.TransportError as e:
        # Below the breaker threshold this becomes a bare 502 with no other
        # trace, yet the client renders a retry banner for it — the 2026-08-20
        # VPN diagnosis meant correlating banners against a log that never
        # mentioned them. Every transport failure gets a line.
        logger.warning("upstream transport failure (%s): %s", type(e).__name__, e)
        if br.record_failure(type(e).__name__):
            return None
        raise HTTPException(status_code=502, detail=f"Anthropic unreachable: {e}") from e
    if uresp.status_code in FAILOVER_STATUSES:
        err_body = await uresp.aread()  # decoded: content-encoding is undone here
        err_headers = _decoded_relay_headers(uresp)
        await uresp.aclose()
        if br.record_failure(f"HTTP {uresp.status_code}", transport_error=False):
            return None
        # Below the threshold: relay the error verbatim so the client's own
        # retry/backoff logic still runs (a lone 429 is normal backpressure).
        return Response(content=err_body, status_code=uresp.status_code, headers=err_headers)
    was_open = br.open
    br.record_success()
    if was_open:
        # The breaker just closed, so every tier it caused to be loaded is now
        # dead weight — unless a failover response is still generating from one.
        await _release_claims(br, settings)
    return _relay_upstream(uresp, settings)


def _model_from_body(body: bytes) -> str:
    try:
        return json.loads(body).get("model", "") or ""
    except Exception:
        return ""


def _mock_response(req: MessagesRequest, text: str) -> MessagesResponse:
    return MessagesResponse(
        id=f"msg_{uuid.uuid4().hex}",
        model=req.model,
        content=[{"type": "text", "text": text}],
        stop_reason="end_turn",
        usage=Usage(input_tokens=10, output_tokens=len(text.split())),
    )


def _check_optimizations(req: MessagesRequest, settings: Settings) -> MessagesResponse | None:
    if settings.skip_quota_probes and is_quota_probe(req):
        logger.debug("intercepted quota probe")
        return _mock_response(req, "ok")

    if settings.skip_title_generation and is_title_generation(req):
        logger.debug("intercepted title generation")
        return _mock_response(req, "Conversation")

    if settings.skip_suggestion_mode and is_suggestion_mode(req):
        logger.debug("intercepted suggestion mode")
        return _mock_response(req, "")

    if settings.mock_prefix_detection:
        hit, cmd = is_prefix_detection(req)
        if hit:
            logger.debug("intercepted prefix detection")
            return _mock_response(req, extract_prefix(cmd))

    if settings.mock_filepath_extraction:
        hit, cmd, output = is_filepath_extraction(req)
        if hit:
            logger.debug("intercepted filepath extraction")
            return _mock_response(req, extract_filepaths(output))

    return None


@router.post("/v1/messages")
async def create_message(
    request: Request,
    settings: Settings = Depends(get_settings),
):
    body = await request.body()
    client: ProviderClient | None = None
    # Set only when the failover path successfully stripped the harness; it then
    # replaces the parsed request below so the stripped version is what is sent.
    bare_req: MessagesRequest | None = None
    # Large fetched documents are compacted once, before bare mode gets a chance
    # to discard their full text. Kept separately for non-bare Qwen profiles.
    prepared_req: MessagesRequest | None = None
    # True only for requests the BREAKER diverted, never for a deliberate
    # `/model qwen`. Decides whether the tier this request loads is one the
    # router may clamp and later evict on its own — see ollama_admin.
    failed_over = False

    if settings.router_mode == "hybrid":
        model = _model_from_body(body)
        profile = resolve_model_route(model)
        if profile is None:
            if not settings.failover_to_local:
                logger.info("→ passthrough [%s] %s", model or "?", request.url.path)
                relayed = await _guarded_passthrough(request, body, settings)
                if relayed is None:
                    raise HTTPException(status_code=502, detail="Anthropic unreachable")
                return relayed
            relay = await _try_upstream(request, body, settings)
            if relay is not None:
                logger.info("→ passthrough [%s] %s", model or "?", request.url.path)
                return relay
            # Failed over. Strip the harness FIRST, then size the tier: what the
            # local model has to prefill is the STRIPPED request, so that is what
            # should choose the profile. Sizing on the raw body would escalate to
            # a big-window tier for a session that, once bare, fits the default
            # one comfortably.
            failed_over = True
            profile = settings.failover_profile
            est = raw_est = None
            try:
                fr = MessagesRequest.model_validate_json(body)
                raw_est = count_messages(fr.messages, fr.system, fr.tools)
                context_settings = load_profile_settings(profile)
                # The router-level QWEN_COGNEE=0 escape hatch must survive the
                # profile load, including true offline failover.
                if not settings.qwen_cognee:
                    context_settings.qwen_cognee = False
                prepared_req = await prepare_external_context(fr, context_settings)
                fr = prepared_req
                if settings.failover_bare:
                    stripped = make_bare(
                        fr,
                        keep=parse_keep(settings.failover_keep_tools),
                        tool_result_chars=settings.failover_tool_result_chars,
                    )
                    bare_req = stripped  # only on success: make_bare is pure
                    fr = stripped
                est = count_messages(fr.messages, fr.system, fr.tools)
                profile = pick_failover_profile(est)
            except Exception:
                # Never let stripping break the failover itself — an unstripped
                # answer beats no answer. The floor profile still serves it.
                logger.exception("bare-mode/sizing failed; falling back to %s", profile)
            logger.warning(
                "⇢ FAILOVER [%s → %s in≈%s (raw %s)] %s (%s)",
                model or "?", profile, est, raw_est, request.url.path,
                get_breaker(settings).reason,
            )
        settings = load_profile_settings(profile)

        # An explicit `/model <name>` hit MODEL_ROUTES and so skipped the
        # failover branch above — the only place that stripped. Tiers whose
        # window assumes bare mode declare ROUTE_BARE; without this a deliberate
        # `/model qwen` sends a full harness session at a 32K window.
        # `not failed_over` keeps this branch exclusive to a deliberate route;
        # failover_bare remains an effective operator escape hatch.
        if not failed_over and bare_req is None and settings.route_bare:
            try:
                fr = MessagesRequest.model_validate_json(body)
                raw_est = count_messages(fr.messages, fr.system, fr.tools)
                prepared_req = await prepare_external_context(fr, settings)
                fr = prepared_req
                stripped = make_bare(
                    fr,
                    keep=parse_keep(settings.failover_keep_tools),
                    tool_result_chars=settings.failover_tool_result_chars,
                )
                bare_req = stripped  # only on success: make_bare is pure
                est = count_messages(stripped.messages, stripped.system, stripped.tools)

                # Size the tier AFTER stripping, the same ordering and the same
                # ladder the failover branch uses. Stripping bounds the prompt but
                # not the transcript, so a long-lived route session outgrows its
                # window; MODEL_ROUTES is static and would keep sending it there
                # forever. Escalate instead of failing.
                if settings.route_max_input_tokens and est > settings.route_max_input_tokens:
                    escalated = pick_failover_profile(est)
                    if escalated != profile:
                        logger.warning(
                            "⇢ ROUTE ESCALATE [%s → %s] in≈%s over %s",
                            profile, escalated, est, settings.route_max_input_tokens,
                        )
                        profile = escalated
                        settings = load_profile_settings(profile)

                logger.info(
                    "→ local [%s → %s] bare in≈%s (raw %s)",
                    model, profile, est, raw_est,
                )
            except Exception:
                # Same rule as failover: stripping must never break the request.
                # Unstripped may overflow the window, but that surfaces as an
                # honest provider error rather than a request we dropped here.
                logger.exception("route bare-mode failed; sending unstripped to %s", profile)

        # Built last: the escalation above can change which profile serves this
        # request, and the client must follow the profile actually chosen.
        client = _get_profile_client(profile, settings)

    if bare_req is not None:
        req = bare_req
    elif prepared_req is not None:
        req = prepared_req
    else:
        req = await prepare_external_context(MessagesRequest.model_validate_json(body), settings)

    fast = _check_optimizations(req, settings)
    if fast:
        return fast

    if client is None:
        client = get_provider_client()

    est_in = count_messages(req.messages, req.system, req.tools)

    # Last-resort tier guard, deliberately placed AFTER every routing branch so
    # no path can skip it. The hybrid branch above already sizes MODEL_ROUTES
    # hits, but "profile" mode never enters that branch at all: it translates
    # every request to the single active profile, with no ladder and no size
    # check anywhere. That is the mode the `qwen` wrapper runs on :8082, and it
    # is the mode the 2026-08-12 pile-up went through — 143,490 tokens sent at a
    # 32K window 87 times over ~17 hours, each attempt reloading the tier.
    # Guarding only the hybrid path fixed a real gap but not that one.
    #
    # Escalation is by MODEL, not profile name, so this stays correct however
    # the tier was chosen and cannot bounce a request to a profile serving the
    # same model. Tiers that opt out (route_max_input_tokens unset, i.e. the
    # wide 64K/256K ones) are unaffected, which also stops a second escalation
    # firing on top of the hybrid branch's.
    if settings.route_max_input_tokens and est_in > settings.route_max_input_tokens:
        try:
            escalated = pick_failover_profile(est_in)
            esettings = load_profile_settings(escalated)
            if esettings.provider_model != settings.provider_model:
                logger.warning(
                    "⇢ TIER ESCALATE [%s → %s] in≈%s over %s",
                    settings.provider_model, esettings.provider_model,
                    est_in, settings.route_max_input_tokens,
                )
                settings = esettings
                client = _get_profile_client(escalated, esettings)
        except Exception:
            # A failed escalation must never take the request with it. Sending
            # it to the original tier reproduces the old behaviour — an honest
            # provider error — which beats dropping it here.
            logger.exception("tier escalation failed; staying on %s", settings.provider_model)

    # Runtime supervision must sit after BOTH router-mode branches and after
    # size escalation. The qwen wrapper uses profile mode on :8082, so keeping
    # this inside the hybrid branch lets its first real request bypass the
    # MLX/Ollama memory interlock whenever direct warmup was skipped.
    if settings.runtime_profile:
        served_by = await mlx_admin.resolve_profile(settings.runtime_profile)
        if served_by != settings.runtime_profile:
            logger.warning(
                "⇢ RUNTIME FALLBACK [%s → %s] %s",
                settings.runtime_profile, served_by, request.url.path,
            )
            settings = load_profile_settings(served_by)
            client = _get_profile_client(served_by, settings)

    if settings.provider_model == "qwen3.8:27b-obliterated":
        compute_lease.claim_exclusive_model(
            settings.provider_model,
            source="claude-failover" if failed_over else "claude-explicit",
            ttl_seconds=600,
        )

    payload = build_nim_payload(req, settings)
    msg_id = f"msg_{uuid.uuid4().hex}"
    last_user = next((m.content for m in reversed(req.messages) if m.role == "user"), "")
    preview = (last_user if isinstance(last_user, str) else str(last_user))[:80]
    mode = "stream" if req.stream else "complete"
    provider = settings.provider_model
    logger.info("→ %s [%s] tools=%s in≈%s | %r", provider, mode, len(req.tools or []), est_in, preview)

    if failed_over:
        # Placed after every escalation branch, so the tier recorded is the one
        # actually about to serve — a TIER ESCALATE above can swap the model out
        # from under the profile chosen at the top, and unloading the wrong name
        # would leave the real 13 GB tier resident.
        br = get_breaker(get_settings())
        br.note_claim(settings.provider_base_url, provider)
        # Re-clamped per request because Ollama resets the timer to the global
        # OLLAMA_KEEP_ALIVE on every inference call; setting it once at load
        # would be undone by the second request of the outage.
        if settings.provider_keep_alive:
            await ollama_admin.set_keep_alive(
                settings.provider_base_url, provider, settings.provider_keep_alive
            )

    if req.stream:
        input_tokens = est_in
        body = _stream(client, payload, msg_id, req, input_tokens, provider,
                       settings.provider_strip_inline_thinking)
        if failed_over:
            # Only the failover path: a deliberate `/model qwen` loads a tier
            # too, but the breaker never claimed it and closing must not
            # release it, so it has nothing to hold open.
            body = _tracked_failover_stream(body, get_settings())
        return StreamingResponse(
            body,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    try:
        resp = await client.complete(payload)
    except ProviderError as e:
        logger.error("Provider error %s: %s", e.status_code, e.message)
        raise HTTPException(status_code=e.status_code, detail=e.message)

    result = nim_response_to_anthropic(
        resp, req, msg_id, settings.provider_strip_inline_thinking
    )
    usage = result.get("usage", {})
    logger.info(
        "← %s [%s] stop=%s out_tokens=%s in_tokens=%s",
        provider, mode,
        result.get("stop_reason"),
        usage.get("output_tokens"),
        usage.get("input_tokens"),
    )
    return result


async def _stream(
    client: ProviderClient,
    payload: dict,
    msg_id: str,
    req: MessagesRequest,
    input_tokens: int,
    provider: str,
    strip_inline_thinking: bool = False,
) -> AsyncIterator[str]:
    # Backends that leave <think> tags in `content` rather than filling
    # `reasoning`; see Settings.provider_strip_inline_thinking.
    state: dict = {"strip_inline_thinking": strip_inline_thinking}
    pending: asyncio.Task | None = None
    try:
        # Open the SSE conversation immediately and heartbeat while waiting:
        # a local model prefilling a big prompt (e.g. 50K-token full-harness
        # session) emits NOTHING for minutes, and a byte-silent stream gets
        # killed + retried by the client at ~120s, doubling every cold prefill.
        for event in start_stream_events(state, msg_id, req, input_tokens):
            yield event

        aiter = client.stream(payload).__aiter__()
        while True:
            if pending is None:
                pending = asyncio.ensure_future(aiter.__anext__())
            try:
                chunk = await asyncio.wait_for(asyncio.shield(pending), timeout=15.0)
            except asyncio.TimeoutError:
                yield 'event: ping\ndata: {"type": "ping"}\n\n'
                continue
            except StopAsyncIteration:
                pending = None
                break
            pending = None
            for event in stream_openai_to_anthropic(chunk, state, msg_id, req, input_tokens):
                yield event
        logger.info("← %s [stream] done in_tokens=%s", provider, input_tokens)
    except ProviderError as e:
        logger.error("Provider stream error %s: %s", e.status_code, e.message)
        yield f"event: error\ndata: {json.dumps({'type':'error','error':{'type':'api_error','message':e.message}})}\n\n"
    finally:
        if pending is not None:
            pending.cancel()


@router.post("/v1/messages/count_tokens")
async def count_tokens(request: Request, settings: Settings = Depends(get_settings)):
    body = await request.body()
    if settings.router_mode == "hybrid" and _model_from_body(body) not in MODEL_ROUTES:
        # While the failover breaker is open, count locally instead of failing.
        if not (settings.failover_to_local and get_breaker(settings).open):
            relayed = await _guarded_passthrough(request, body, settings)
            if relayed is not None:
                return relayed
            # Upstream is unreachable. Fall through to the local counter rather
            # than failing the request: counting is arithmetic over the body, so
            # unlike a completion it needs no model, no tier and no GPU. This is
            # the one passthrough that can always answer offline, and answering
            # keeps a session usable through a blip the breaker has not yet
            # opened on.
    req = TokenCountRequest.model_validate_json(body)
    return TokenCountResponse(input_tokens=count_messages(req.messages, req.system, req.tools))


@router.get("/health")
async def health():
    return {"status": "ok"}


# Catch-all LAST: in hybrid mode any endpoint we don't handle locally
# (/v1/models, future API surfaces, …) forwards to the real Anthropic API so
# the proxy is transparent to Claude Code.
@router.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def passthrough_any(path: str, request: Request, settings: Settings = Depends(get_settings)):
    if settings.router_mode != "hybrid":
        raise HTTPException(status_code=404, detail="Not found")
    relayed = await _guarded_passthrough(request, await request.body(), settings)
    if relayed is None:
        # No local equivalent for an arbitrary endpoint, so the failure has to
        # surface — but as a 502 the client can retry, not a dropped connection.
        raise HTTPException(status_code=502, detail="Anthropic unreachable")
    return relayed
