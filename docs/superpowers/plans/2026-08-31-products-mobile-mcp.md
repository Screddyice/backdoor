# Account-synced products MCP

## Goal

Expose HyperCrawl, HyperScale, and EngageMate through one remote OAuth connector that Claude can load on desktop, web, and mobile.

## What already exists

- `src/hermes_mcp/oauth.py` supplies Claude-compatible dynamic registration, PKCE, token rotation, and restart-safe state. The bridge reuses it.
- HyperScale publishes a native Streamable HTTP MCP endpoint with 34 tools.
- HyperCrawl and EngageMate publish OpenAPI documents. The bridge derives their callable operations from those documents.
- The server already stores each upstream credential in a protected environment file. The bridge reads those files in place.

## Data flow

```text
Claude account
    |
    | OAuth access token
    v
Products MCP :8010
    |-- HyperCrawl REST /v1     bearer tenant key
    |-- HyperScale /mcp         organization API key
    `-- EngageMate API :13100   internal key plus user ID
```

The public connector exposes three tools per product: status, list tools, and call. A call may target only an operation returned by the upstream catalog. The bridge does not accept caller-supplied URLs or headers.

## Failure modes

| Code path | Failure | Test | User result |
| --- | --- | --- | --- |
| OpenAPI catalog | Invalid or missing document | Yes | MCP error naming the product |
| REST call | Unknown tool or misspelled argument | Yes | MCP error before network access |
| Native MCP call | HTTP or JSON-RPC failure | Yes | Status or error code without upstream body |
| OAuth | Invalid redirect, password, code, or refresh token | Existing Hermes OAuth suite | Authorization fails closed |
| Deployment | Product service unavailable | Live status call | Product status fails while other products remain usable |

## NOT in scope

- Publishing three separate Claude connectors. One owner-facing connector keeps authorization and phone setup to one account row.
- Replacing each product's tenant authorization. The gateway preserves the current product boundary.
- Copying raw credentials into the repository or Claude connector form.

## Implementation Tasks

- [x] **T1 (P1, human: ~4h / CC: ~30min)** - Gateway client - Forward native MCP and catalog-derived REST operations.
  - Surfaced by: Architecture review, preserve existing product auth and schemas.
  - Files: `src/products_mcp/client.py`, `tests/test_products_mcp_client.py`
  - Verify: `uv run pytest -q tests/test_products_mcp_client.py`
- [x] **T2 (P1, human: ~3h / CC: ~20min)** - OAuth server - Register nine namespaced gateway tools.
  - Surfaced by: Architecture review, Claude mobile requires an account-synced remote OAuth MCP.
  - Files: `src/products_mcp/server.py`, `tests/test_products_mcp_server.py`
  - Verify: `uv run pytest -q tests/test_products_mcp_server.py`
- [ ] **T3 (P1, human: ~2h / CC: ~20min)** - Deployment - Publish, connect, reload, and call one harmless tool per product.
  - Surfaced by: Test review, protocol checks do not prove account persistence.
  - Files: `deploy/products-mcp-http.service`, remote Caddy and service state
  - Verify: OAuth initialize, `tools/list`, three status calls, Claude reload, phone connector row

Sequential implementation, no parallelization opportunity.

## GSTACK REVIEW REPORT

| Runs | Status | Findings |
| --- | --- | --- |
| Scope challenge | accepted | Reuse the existing OAuth provider and upstream catalogs |
| Architecture | clear | One connector, nine namespaced tools, no credential copies |
| Code quality | clear | Explicit product routing and fail-closed operation lookup |
| Tests | clear | Catalog, call, auth header, redaction, and argument failures covered |
| Performance | clear | One HTTP hop; OpenAPI catalogs cached for the process lifetime |

VERDICT: READY TO IMPLEMENT AND DEPLOY

NO UNRESOLVED DECISIONS

