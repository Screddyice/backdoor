"""End-to-end OAuth coverage for Claude's remote MCP connector flow."""

import base64
import hashlib
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.testclient import TestClient

from src.hermes_mcp.http_server import build_server
from src.hermes_mcp.oauth import OAuthConfigError
from src.hermes_mcp.registry import Profile

REGISTRY = {
    "screddy": Profile(
        name="screddy",
        tier="full",
        port=8643,
        key_env="SCREDDY_API_KEY",
        unit="hermes-gateway.service",
        home="/home/hermes/.hermes",
    )
}
ISSUER = "http://127.0.0.1:8000"
PASSWORD = "correct horse battery staple"
REDIRECT_URI = "https://client.example/callback"
ACCEPT_HEADERS = {"Accept": "application/json, text/event-stream"}
INITIALIZE_BODY = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "claude-connector-test", "version": "1"},
    },
}


def _oauth_env(monkeypatch, state_path: Path) -> None:
    monkeypatch.delenv("HERMES_MCP_KEY", raising=False)
    monkeypatch.setenv("HERMES_MCP_OAUTH_ISSUER", ISSUER)
    monkeypatch.setenv("HERMES_MCP_OAUTH_PASSWORD", PASSWORD)
    monkeypatch.setenv("HERMES_MCP_OAUTH_STATE_PATH", str(state_path))


def _register(client: TestClient) -> dict:
    response = client.post(
        "/register",
        json={
            "client_name": "Claude",
            "redirect_uris": [REDIRECT_URI],
            "token_endpoint_auth_method": "client_secret_post",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "scope": "hermes",
        },
    )
    assert response.status_code == 201
    return response.json()


def _authorize(client: TestClient, registration: dict) -> tuple[str, str]:
    verifier = "oauth-test-verifier-with-enough-entropy-0123456789"
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    response = client.get(
        "/authorize",
        params={
            "client_id": registration["client_id"],
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "claude-state",
            "scope": "hermes",
            "resource": f"{ISSUER}/mcp",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302
    login_state = parse_qs(urlparse(response.headers["location"]).query)["state"][0]
    return login_state, verifier


def test_oauth_mode_supports_claude_login_tokens_refresh_and_mcp(monkeypatch, tmp_path):
    state_path = tmp_path / "oauth-state.json"
    _oauth_env(monkeypatch, state_path)
    server = build_server(REGISTRY)
    app = server.streamable_http_app(stateless_http=True, json_response=True)

    with TestClient(app, base_url=ISSUER) as client:
        metadata = client.get("/.well-known/oauth-authorization-server")
        assert metadata.status_code == 200
        assert metadata.json()["issuer"] == f"{ISSUER}/"
        assert metadata.json()["registration_endpoint"] == f"{ISSUER}/register"

        registration = _register(client)
        login_state, verifier = _authorize(client, registration)

        login_page = client.get("/login", params={"state": login_state})
        assert login_page.status_code == 200
        assert "Screddy Hermes" in login_page.text

        denied = client.post(
            "/login",
            data={"state": login_state, "password": "wrong-password"},
            follow_redirects=False,
        )
        assert denied.status_code == 401
        assert PASSWORD not in denied.text

        for _ in range(4):
            assert (
                client.post(
                    "/login",
                    data={"state": login_state, "password": "wrong-password"},
                    follow_redirects=False,
                ).status_code
                == 401
            )
        throttled = client.post(
            "/login",
            data={"state": login_state, "password": PASSWORD},
            follow_redirects=False,
        )
        assert throttled.status_code == 429

        # Use a fresh server to keep the remainder of the OAuth flow independent
        # of the deliberate brute-force lockout above.

    server = build_server(REGISTRY)
    app = server.streamable_http_app(stateless_http=True, json_response=True)
    with TestClient(app, base_url=ISSUER) as client:
        registration = _register(client)
        login_state, verifier = _authorize(client, registration)

        approved = client.post(
            "/login",
            data={"state": login_state, "password": PASSWORD},
            follow_redirects=False,
        )
        assert approved.status_code == 302
        redirect = urlparse(approved.headers["location"])
        query = parse_qs(redirect.query)
        assert f"{redirect.scheme}://{redirect.netloc}{redirect.path}" == REDIRECT_URI
        assert query["state"] == ["claude-state"]

        token_response = client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": query["code"][0],
                "redirect_uri": REDIRECT_URI,
                "client_id": registration["client_id"],
                "client_secret": registration["client_secret"],
                "code_verifier": verifier,
                "resource": f"{ISSUER}/mcp",
            },
        )
        assert token_response.status_code == 200
        tokens = token_response.json()
        assert tokens["access_token"]
        assert tokens["refresh_token"]

        initialized = client.post(
            "/mcp",
            json=INITIALIZE_BODY,
            headers={
                **ACCEPT_HEADERS,
                "Authorization": f"Bearer {tokens['access_token']}",
            },
        )
        assert initialized.status_code == 200

    assert state_path.stat().st_mode & 0o777 == 0o600
    assert PASSWORD not in state_path.read_text(encoding="utf-8")

    # A service restart must retain Claude's registration and refresh token.
    restarted = build_server(REGISTRY)
    restarted_app = restarted.streamable_http_app(
        stateless_http=True, json_response=True
    )
    with TestClient(restarted_app, base_url=ISSUER) as client:
        refreshed = client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "client_id": registration["client_id"],
                "client_secret": registration["client_secret"],
                "scope": "hermes",
                "resource": f"{ISSUER}/mcp",
            },
        )
        assert refreshed.status_code == 200
        assert refreshed.json()["access_token"] != tokens["access_token"]
        assert refreshed.json()["refresh_token"] != tokens["refresh_token"]


@pytest.mark.parametrize("password", [None, "short", "aaaaaaaaaaaaaaaa"])
def test_oauth_mode_refuses_a_missing_or_weak_login_password(
    monkeypatch, tmp_path, password
):
    monkeypatch.delenv("HERMES_MCP_KEY", raising=False)
    monkeypatch.setenv("HERMES_MCP_OAUTH_ISSUER", ISSUER)
    monkeypatch.setenv("HERMES_MCP_OAUTH_STATE_PATH", str(tmp_path / "state.json"))
    if password is None:
        monkeypatch.delenv("HERMES_MCP_OAUTH_PASSWORD", raising=False)
    else:
        monkeypatch.setenv("HERMES_MCP_OAUTH_PASSWORD", password)

    with pytest.raises(OAuthConfigError):
        build_server(REGISTRY)


def test_oauth_mode_refuses_plain_http_for_a_public_issuer(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_MCP_KEY", raising=False)
    monkeypatch.setenv("HERMES_MCP_OAUTH_ISSUER", "http://hermes.example.com")
    monkeypatch.setenv("HERMES_MCP_OAUTH_PASSWORD", PASSWORD)
    monkeypatch.setenv("HERMES_MCP_OAUTH_STATE_PATH", str(tmp_path / "state.json"))

    with pytest.raises(OAuthConfigError):
        build_server(REGISTRY)
