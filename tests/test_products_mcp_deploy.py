"""Deployment contract for the account-synced products MCP service."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_products_runtime_overrides_load_after_shared_credential_files() -> None:
    unit = (ROOT / "deploy/products-mcp-http.service").read_text()
    runtime_file = "%h/backdoor-products-mcp/deploy/products-mcp-runtime.env"

    assert runtime_file in unit
    assert unit.index(runtime_file) > unit.index("/root/engagemate/src/deploy/.env.api")

    runtime = (ROOT / "deploy/products-mcp-runtime.env").read_text()
    assert "HERMES_MCP_PORT=8010" in runtime
    assert "HERMES_MCP_HOST=127.0.0.1" in runtime
    assert (
        "HERMES_MCP_ALLOWED_HOSTS="
        "screddy-products.5-161-126-205.sslip.io,"
        "screddy-products.5-161-126-205.sslip.io:443"
    ) in runtime
