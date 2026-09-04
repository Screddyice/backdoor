import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import importlib.util


ROOT = Path(__file__).parents[1]
HELPER = ROOT / "local" / "qwen-mcp-config.py"


def load_helper_module():
    spec = importlib.util.spec_from_file_location("qwen_mcp_config", HELPER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def run_helper(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HELPER), *args],
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.fixture
def source_config(tmp_path: Path) -> Path:
    path = tmp_path / "claude.json"
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "alpha": {"type": "http", "url": "https://alpha.test/mcp"},
                    "beta": {
                        "command": "beta-mcp",
                        "env": {"TOKEN": "secret-value"},
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    return path


def test_list_prints_names_without_configuration_values(source_config: Path):
    result = run_helper("--config", str(source_config), "list")
    assert result.returncode == 0
    assert result.stdout.splitlines() == ["alpha", "beta"]
    assert "secret-value" not in result.stdout


def test_build_includes_only_requested_servers(source_config: Path, tmp_path: Path):
    output = tmp_path / "selected.json"
    result = run_helper(
        "--config", str(source_config), "build", "--servers", "beta,beta", "--output", str(output)
    )
    assert result.returncode == 0
    assert json.loads(output.read_text(encoding="utf-8")) == {
        "mcpServers": {
            "beta": {"command": "beta-mcp", "env": {"TOKEN": "secret-value"}}
        }
    }
    assert os.stat(output).st_mode & 0o777 == 0o600


def test_build_can_keep_memory_with_the_requested_server(
    source_config: Path, tmp_path: Path
):
    output = tmp_path / "selected.json"
    shim = tmp_path / "cmem-mcp-shim.py"
    result = run_helper(
        "--config",
        str(source_config),
        "build",
        "--servers",
        "alpha",
        "--output",
        str(output),
        "--memory-shim",
        str(shim),
    )
    assert result.returncode == 0
    config = json.loads(output.read_text(encoding="utf-8"))["mcpServers"]
    assert set(config) == {"alpha", "cmem"}
    assert config["cmem"] == {
        "type": "stdio",
        "command": "python3",
        "args": [str(shim)],
    }


def test_unknown_server_fails_without_writing_config(source_config: Path, tmp_path: Path):
    output = tmp_path / "selected.json"
    result = run_helper(
        "--config", str(source_config), "build", "--servers", "missing", "--output", str(output)
    )
    assert result.returncode != 0
    assert "unknown MCP server(s): missing" in result.stderr
    assert "secret-value" not in result.stderr
    assert not output.exists()


def test_empty_selection_is_rejected(source_config: Path):
    result = run_helper("--config", str(source_config), "validate", "--servers", ",")
    assert result.returncode != 0
    assert "select at least one MCP server" in result.stderr


def test_connectivity_probe_accepts_a_verified_tls_peer(monkeypatch):
    helper = load_helper_module()

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def settimeout(self, _timeout):
            return None

    class Context:
        def wrap_socket(self, connection, server_hostname):
            assert server_hostname == "one.one.one.one"
            return connection

    monkeypatch.setattr(helper.socket, "create_connection", lambda *_a, **_k: Connection())
    monkeypatch.setattr(helper.ssl, "create_default_context", lambda: Context())
    assert helper.internet_reachable() is True


def test_connectivity_probe_tries_every_peer_before_offline(monkeypatch):
    helper = load_helper_module()
    attempted = []

    def refuse(address, **_kwargs):
        attempted.append(address)
        raise OSError("offline")

    monkeypatch.setattr(helper.socket, "create_connection", refuse)
    assert helper.internet_reachable() is False
    assert attempted == [("1.1.1.1", 443), ("8.8.8.8", 443)]
