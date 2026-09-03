#!/usr/bin/env python3
"""Select named MCP servers for one Qwen session without exposing credentials."""

from __future__ import annotations

import argparse
import json
import os
import socket
import ssl
from pathlib import Path


CONNECTIVITY_PROBES = (
    ("1.1.1.1", 443, "one.one.one.one"),
    ("8.8.8.8", 443, "dns.google"),
)


def load_servers(config_path: Path) -> dict[str, dict]:
    with config_path.open(encoding="utf-8") as handle:
        config = json.load(handle)
    servers = config.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError("mcpServers must be an object")
    return servers


def parse_names(raw: str) -> list[str]:
    names = [name.strip() for name in raw.split(",") if name.strip()]
    if not names:
        raise ValueError("select at least one MCP server")
    return list(dict.fromkeys(names))


def select_servers(servers: dict[str, dict], raw_names: str) -> dict[str, dict]:
    names = parse_names(raw_names)
    missing = [name for name in names if name not in servers]
    if missing:
        available = ", ".join(sorted(servers)) or "none"
        raise ValueError(
            f"unknown MCP server(s): {', '.join(missing)}; available: {available}"
        )
    return {name: servers[name] for name in names}


def write_private_config(output_path: Path, selected: dict[str, dict]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"mcpServers": selected}, handle, indent=2)
            handle.write("\n")
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    os.chmod(temporary, 0o600)
    os.replace(temporary, output_path)


def internet_reachable(timeout: float = 2.0) -> bool:
    """Verify a public TLS peer without depending on DNS or project packages."""
    context = ssl.create_default_context()
    for host, port, certificate_name in CONNECTIVITY_PROBES:
        try:
            connection = socket.create_connection((host, port), timeout=timeout)
        except OSError:
            continue
        with connection:
            connection.settimeout(timeout)
            try:
                with context.wrap_socket(
                    connection, server_hostname=certificate_name
                ):
                    return True
            except OSError:
                continue
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config", type=Path, default=Path.home() / ".claude.json"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list")
    subparsers.add_parser("online")

    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("--servers", required=True)

    build_parser = subparsers.add_parser("build")
    build_parser.add_argument("--servers", required=True)
    build_parser.add_argument("--output", type=Path, required=True)
    build_parser.add_argument("--cognee-shim", type=Path)

    args = parser.parse_args()
    if args.command == "online":
        return 0 if internet_reachable() else 1

    servers = load_servers(args.config)
    if args.command == "list":
        for name in sorted(servers):
            print(name)
        return 0

    selected = select_servers(servers, args.servers)
    if args.command == "build":
        if args.cognee_shim:
            selected["cognee"] = {
                "type": "stdio",
                "command": "python3",
                "args": [str(args.cognee_shim.expanduser())],
            }
        write_private_config(args.output, selected)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise SystemExit(f"qwen: {error}") from error
