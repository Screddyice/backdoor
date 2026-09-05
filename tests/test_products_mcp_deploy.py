"""Deployment contract for the account-synced product MCP services."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _environment(path: Path) -> dict[str, str]:
    return {
        name: value
        for line in path.read_text().splitlines()
        if line and not line.startswith("#")
        for name, value in [line.split("=", 1)]
    }


def test_product_runtime_overrides_load_after_shared_credential_files() -> None:
    unit = (ROOT / "deploy/products-mcp-http@.service").read_text()
    credential_file = "%h/.config/products-mcp/%i.env"
    runtime_file = "%h/backdoor-products-mcp/deploy/products-mcp-%i.env"

    assert "/root/" not in unit
    assert credential_file in unit
    assert runtime_file in unit
    assert unit.index(runtime_file) > unit.index(credential_file)


def test_product_runtimes_have_unique_oauth_and_network_boundaries() -> None:
    products = {
        "hypercrawl": ("8011", "hypercrawl-mcp.5-161-126-205.sslip.io"),
        "hyperscale": ("8012", "hyperscale-mcp.5-161-126-205.sslip.io"),
        "engagemate": ("8013", "engagemate-mcp.5-161-126-205.sslip.io"),
    }

    for product, (port, hostname) in products.items():
        runtime = _environment(ROOT / f"deploy/products-mcp-{product}.env")

        assert runtime["PRODUCTS_MCP_PRODUCT"] == product
        assert runtime["HERMES_MCP_PORT"] == port
        assert runtime["HERMES_MCP_HOST"] == "127.0.0.1"
        assert runtime["HERMES_MCP_OAUTH_ISSUER"] == f"https://{hostname}"
        assert runtime["HERMES_MCP_OAUTH_STATE_PATH"].endswith(
            f"/{product}-oauth-state.json"
        )
        assert runtime["HERMES_MCP_ALLOWED_HOSTS"] == f"{hostname},{hostname}:443"
