#!/usr/bin/env python3
"""Run the context candidate without changing machine-wide routing or services."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import socket
import sys
import tempfile
import time
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.proxy.routes as routes
from src.proxy.app import create_app
from src.proxy.config import Settings, get_settings
from src.proxy.context_tokenizer import TokenCount
from src.proxy.failover import FailoverBreaker


PROTECTED_PATHS = (
    Path("~/.claude/settings.json").expanduser(),
    Path("~/.claude/statusline.sh").expanduser(),
    Path("~/.codex/config.toml").expanduser(),
    Path("~/Library/LaunchAgents/com.screddy.backdoor-router.plist").expanduser(),
)


class CloudStream(httpx.AsyncByteStream):
    def __init__(self, body: bytes):
        self.body = body

    async def __aiter__(self):
        yield self.body

    async def aclose(self):
        return None


class SwitchingTransport(httpx.AsyncBaseTransport):
    def __init__(self):
        self.offline = False
        self.calls = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.offline:
            raise httpx.ConnectError("candidate transport offline", request=request)
        body = json.dumps({
            "provider": "anthropic",
            "answer": f"cloud-{self.calls}",
        }, separators=(",", ":")).encode()
        return httpx.Response(
            200,
            stream=CloudStream(body),
            headers={"content-type": "application/json"},
            request=request,
        )


class CandidateProvider:
    def __init__(self):
        self.calls = 0
        self.payloads: list[dict[str, Any]] = []

    async def complete(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        self.payloads.append(payload)
        return {
            "choices": [{
                "finish_reason": "stop",
                "message": {
                    "content": "The saved rollback revision remains 621d765.",
                    "tool_calls": [],
                },
            }],
            "usage": {
                "prompt_tokens": 18_000,
                "completion_tokens": 8,
            },
        }

    async def stream(self, payload: dict[str, Any]):
        self.calls += 1
        self.payloads.append(payload)
        yield {
            "choices": [{
                "delta": {"content": "The saved rollback revision remains 621d765."},
                "finish_reason": None,
            }],
        }
        yield {
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 18_000, "completion_tokens": 8},
        }


class CandidateGate:
    def __init__(self):
        self.counts: list[int] = []

    def fits(self, payload: dict[str, Any], hard_limit: int):
        measured = max(1, len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) // 4)
        count = min(measured, hard_limit)
        self.counts.append(count)
        return True, TokenCount(value=count, source="utf8-bytes", exact=False)


def fingerprint(path: Path) -> tuple[str, int] | None:
    if not path.exists() or not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mode


def reserve_loopback_ports(count: int = 2) -> tuple[list[socket.socket], list[int]]:
    sockets = []
    ports = []
    for _ in range(count):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        sockets.append(listener)
        ports.append(listener.getsockname()[1])
    return sockets, ports


def long_request(synthetic_tokens: int = 507_000) -> dict[str, Any]:
    filler_words = max(30_000, synthetic_tokens)
    return {
        "model": "claude-opus-5",
        "max_tokens": 8_192,
        "tools": [
            {"name": "Read", "description": "read", "input_schema": {}},
            {"name": "Glob", "description": "glob", "input_schema": {}},
            {"name": "Grep", "description": "grep", "input_schema": {}},
            {"name": "Bash", "description": "shell", "input_schema": {}},
            {"name": "Edit", "description": "edit", "input_schema": {}},
        ],
        "messages": [
            {"role": "user", "content": "The rollback revision was 621d765."},
            {"role": "assistant", "content": "recorded"},
            {"role": "user", "content": "filler " * filler_words},
            {"role": "assistant", "content": "older session material"},
            {"role": "user", "content": "Which rollback revision did we record?"},
        ],
    }


async def post(app, body: dict[str, Any]) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://candidate",
    ) as client:
        return await client.post(
            "/v1/messages",
            content=json.dumps(body).encode(),
            headers={"content-type": "application/json"},
        )


async def run_candidate(temp_root: Path | None = None) -> dict[str, Any]:
    owned_temp = temp_root is None
    root = Path(temp_root) if temp_root is not None else Path(
        tempfile.mkdtemp(prefix="backdoor-context-candidate-")
    )
    root.mkdir(parents=True, exist_ok=True)
    before = {str(path): fingerprint(path) for path in PROTECTED_PATHS}
    listeners, ports = reserve_loopback_ports()

    transport = SwitchingTransport()
    upstream = httpx.AsyncClient(
        base_url="https://api.anthropic.com",
        transport=transport,
    )
    provider = CandidateProvider()
    gate = CandidateGate()
    admin_calls: list[tuple[str, str]] = []

    saved = {
        "upstream": routes._upstream_client,
        "breaker": routes._breaker,
        "runtimes": routes._context_runtimes,
        "get_profile": routes._get_profile_client,
        "get_gate": routes._get_token_gate,
        "load_profile": routes.load_profile_settings,
        "resolve_profile": routes.mlx_admin.resolve_profile,
        "unload": routes.ollama_admin.unload,
        "keep_alive": routes.ollama_admin.set_keep_alive,
        "inflight": routes._failover_inflight,
        "deferred": routes._deferred_unloads,
    }

    real_load = routes.load_profile_settings

    def isolated_profile(profile: str):
        settings = real_load(profile).model_copy(deep=True)
        settings.memory_inject = False
        settings.qwen_cognee = False
        return settings

    async def preserve_profile(profile: str) -> str:
        return profile

    async def record_unload(base_url: str, model: str) -> bool:
        admin_calls.append(("unload", model))
        return True

    async def record_keep_alive(base_url: str, model: str, keep_alive: str) -> bool:
        admin_calls.append(("keep_alive", model))
        return True

    routes._upstream_client = upstream
    routes._breaker = FailoverBreaker(
        threshold=1,
        probe_interval=0,
        recovery_successes=2,
        online_fn=lambda: False,
        notify_fn=lambda *_: None,
        state_path=root / "failover-state.json",
    )
    routes._context_runtimes = {}
    routes._get_profile_client = lambda _profile, _settings: provider
    routes._get_token_gate = lambda _settings: gate
    routes.load_profile_settings = isolated_profile
    routes.mlx_admin.resolve_profile = preserve_profile
    routes.ollama_admin.unload = record_unload
    routes.ollama_admin.set_keep_alive = record_keep_alive
    routes._failover_inflight = 0
    routes._deferred_unloads = set()

    settings = Settings(
        router_mode="hybrid",
        failover_to_local=True,
        failover_threshold=1,
        failover_probe_seconds=0,
        failover_recovery_successes=2,
        failover_first_text_seconds=2.0,
        failover_total_seconds=4.0,
        context_virtualization=True,
        context_store_path=str(root / "transcripts.sqlite3"),
        context_target_input_tokens=18_000,
        context_hard_input_tokens=22_000,
        context_retrieval_tokens=5_000,
        qwen_cognee=False,
        memory_inject=False,
    )
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    report: dict[str, Any] = {"loopback_ports": ports}

    try:
        cloud_response = await post(app, {
            "model": "claude-opus-5",
            "messages": [{"role": "user", "content": "candidate cloud turn"}],
        })
        report["cloud"] = cloud_response.json()

        outage_body = long_request()
        parsed_outage = routes.MessagesRequest.model_validate(outage_body)
        synthetic_input_tokens = routes.count_messages(
            parsed_outage.messages,
            parsed_outage.system,
            parsed_outage.tools,
        )
        transport.offline = True
        started = time.monotonic()
        local_response = await post(app, outage_body)
        elapsed = time.monotonic() - started
        local_json = local_response.json()
        local_answer = " ".join(
            block.get("text", "") for block in local_json.get("content", [])
            if block.get("type") == "text"
        )
        local_payload = provider.payloads[-1]
        report["local"] = {
            "provider": "qwen3.8:27b-obliterated",
            "answer": local_answer,
            "synthetic_input_tokens": synthetic_input_tokens,
            "final_input_tokens": gate.counts[-1],
            "max_output_tokens": local_payload["max_tokens"],
            "first_text_seconds": elapsed,
            "total_seconds": elapsed,
            "local_calls": provider.calls,
        }

        upstream_before_retry = transport.calls
        transport.offline = False
        cached_response = await post(app, outage_body)
        cached_json = cached_response.json()
        cached_answer = " ".join(
            block.get("text", "") for block in cached_json.get("content", [])
            if block.get("type") == "text"
        )
        report["cached_retry"] = {
            "answer": cached_answer,
            "local_calls": provider.calls,
            "upstream_calls_unchanged": transport.calls == upstream_before_retry,
        }

        probe = await post(app, {
            "model": "claude-opus-5",
            "messages": [{"role": "user", "content": "recovery probe one"}],
        })
        report["recovery_probe"] = {
            **probe.json(),
            "breaker_open": routes._breaker.open,
        }
        recovered = await post(app, {
            "model": "claude-opus-5",
            "messages": [{"role": "user", "content": "recovery probe two"}],
        })
        report["recovered"] = {
            **recovered.json(),
            "breaker_open": routes._breaker.open,
        }

        runtime = next(iter(routes._context_runtimes.values()))
        original_archive = runtime.store.archive_request

        def fail_archive(_request):
            raise OSError("candidate sqlite fault")

        runtime.store.archive_request = fail_archive
        transport.offline = True
        fault_response = await post(app, {
            "model": "claude-opus-5",
            "messages": [{"role": "user", "content": "new turn during sqlite fault"}],
        })
        runtime.store.archive_request = original_archive
        report["sqlite_fault"] = {
            "continuity": "local inference could not finish" in fault_response.text,
            "local_calls": provider.calls,
        }
    finally:
        app.dependency_overrides.clear()
        for runtime in list(routes._context_runtimes.values()):
            await runtime.close()
        await upstream.aclose()
        routes._upstream_client = saved["upstream"]
        routes._breaker = saved["breaker"]
        routes._context_runtimes = saved["runtimes"]
        routes._get_profile_client = saved["get_profile"]
        routes._get_token_gate = saved["get_gate"]
        routes.load_profile_settings = saved["load_profile"]
        routes.mlx_admin.resolve_profile = saved["resolve_profile"]
        routes.ollama_admin.unload = saved["unload"]
        routes.ollama_admin.set_keep_alive = saved["keep_alive"]
        routes._failover_inflight = saved["inflight"]
        routes._deferred_unloads = saved["deferred"]
        for listener in listeners:
            listener.close()

    after = {str(path): fingerprint(path) for path in PROTECTED_PATHS}
    report["protected_changes"] = [
        path for path in before if before[path] != after[path]
    ]
    report["global_configuration_unchanged"] = not report["protected_changes"]
    report["admin_calls_intercepted"] = admin_calls
    report["modified_paths"] = sorted(
        str(path) for path in root.rglob("*") if path.is_file()
    )
    if owned_temp:
        for path in sorted(root.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
        root.rmdir()
    return report


def main() -> int:
    report = asyncio.run(run_candidate())
    checks = {
        "cloud": report["cloud"].get("provider") == "anthropic",
        "bounded_local": report["local"]["final_input_tokens"] <= 22_000,
        "stable_retry": (
            report["cached_retry"]["answer"] == report["local"]["answer"]
            and report["cached_retry"]["upstream_calls_unchanged"]
        ),
        "two_probe_recovery": (
            report["recovery_probe"]["breaker_open"]
            and not report["recovered"]["breaker_open"]
        ),
        "sqlite_fault_continuity": report["sqlite_fault"]["continuity"],
        "global_configuration": report["global_configuration_unchanged"],
    }
    for name, passed in checks.items():
        print(f"{'PASS' if passed else 'FAIL'} {name}")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
