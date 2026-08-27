"""End-to-end OAuth coverage for Claude's remote MCP connector flow."""

import base64
import hashlib
import json
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
    monkeypatch.setenv("HERMES_MCP_OAUTH_REDIRECT_HOSTS", "client.example")


def _register(
    client: TestClient, redirect_uri: str = REDIRECT_URI, expected_status: int = 201
) -> dict:
    response = client.post(
        "/register",
        json={
            "client_name": "Claude",
            "redirect_uris": [redirect_uri],
            "token_endpoint_auth_method": "client_secret_post",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "scope": "hermes",
        },
    )
    assert response.status_code == expected_status
    return response.json()


def _authorize(
    client: TestClient,
    registration: dict,
    *,
    resource: str | None = f"{ISSUER}/mcp",
) -> tuple[str, str]:
    verifier = "oauth-test-verifier-with-enough-entropy-0123456789"
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    params = {
        "client_id": registration["client_id"],
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "claude-state",
        "scope": "hermes",
    }
    if resource is not None:
        params["resource"] = resource
    response = client.get(
        "/authorize",
        params=params,
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
        missing_auth = client.post("/mcp", json=INITIALIZE_BODY, headers=ACCEPT_HEADERS)
        assert missing_auth.status_code == 401
        assert "resource_metadata=" in missing_auth.headers["www-authenticate"]

        invalid_auth = client.post(
            "/mcp",
            json=INITIALIZE_BODY,
            headers={**ACCEPT_HEADERS, "Authorization": "Bearer not-a-token"},
        )
        assert invalid_auth.status_code == 401

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
            data={"state": login_state, "password": "wrong-password"},
            follow_redirects=False,
        )
        assert throttled.status_code == 429

        still_approved = client.post(
            "/login",
            data={"state": login_state, "password": PASSWORD},
            follow_redirects=False,
        )
        assert still_approved.status_code == 302

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
        refreshed_tokens = refreshed.json()
        assert refreshed_tokens["access_token"] != tokens["access_token"]
        assert refreshed_tokens["refresh_token"] != tokens["refresh_token"]

        replayed_refresh = client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "client_id": registration["client_id"],
                "client_secret": registration["client_secret"],
                "scope": "hermes",
            },
        )
        assert replayed_refresh.status_code == 400
        assert replayed_refresh.json()["error"] == "invalid_grant"

        initialized = client.post(
            "/mcp",
            json=INITIALIZE_BODY,
            headers={
                **ACCEPT_HEADERS,
                "Authorization": f"Bearer {refreshed_tokens['access_token']}",
            },
        )
        assert initialized.status_code == 200

    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert (
        persisted["access_tokens"][refreshed_tokens["access_token"]]["resource"]
        == f"{ISSUER}/mcp"
    )


def test_login_throttle_is_global_but_never_blocks_the_correct_password(
    monkeypatch, tmp_path
):
    _oauth_env(monkeypatch, tmp_path / "state.json")
    app = build_server(REGISTRY).streamable_http_app(
        stateless_http=True, json_response=True
    )
    with TestClient(app, base_url=ISSUER) as client:
        attacker = _register(client)
        attacker_state, _ = _authorize(client, attacker)
        owner = _register(client)
        owner_state, _ = _authorize(client, owner)

        for _ in range(5):
            response = client.post(
                "/login",
                data={"state": attacker_state, "password": "wrong-password"},
                follow_redirects=False,
            )
            assert response.status_code == 401

        throttled_wrong = client.post(
            "/login",
            data={"state": owner_state, "password": "wrong-password"},
            follow_redirects=False,
        )
        assert throttled_wrong.status_code == 429

        approved = client.post(
            "/login",
            data={"state": owner_state, "password": PASSWORD},
            follow_redirects=False,
        )
        assert approved.status_code == 302


def test_registration_rejects_unapproved_redirect_and_is_bounded(monkeypatch, tmp_path):
    _oauth_env(monkeypatch, tmp_path / "state.json")
    monkeypatch.setenv("HERMES_MCP_OAUTH_REDIRECT_HOSTS", "client.example")
    monkeypatch.setattr("src.hermes_mcp.oauth.MAX_REGISTERED_CLIENTS", 1)
    app = build_server(REGISTRY).streamable_http_app(
        stateless_http=True, json_response=True
    )
    with TestClient(app, base_url=ISSUER) as client:
        rejected = _register(
            client, "https://attacker.example/callback", expected_status=400
        )
        assert rejected["error"] == "invalid_redirect_uri"

        first = _register(client)
        second = _register(client)
        persisted = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
        assert len(persisted["clients"]) == 1
        assert first["client_id"] not in persisted["clients"]
        assert second["client_id"] in persisted["clients"]


@pytest.mark.parametrize(
    "redirect_uri",
    [
        "evil://client.example/callback",
        "javascript://client.example/callback",
        "ftp://client.example/callback",
        "http://client.example/callback",
    ],
)
def test_registration_rejects_unsafe_redirect_schemes(
    monkeypatch, tmp_path, redirect_uri
):
    _oauth_env(monkeypatch, tmp_path / "state.json")
    app = build_server(REGISTRY).streamable_http_app(
        stateless_http=True, json_response=True
    )
    with TestClient(app, base_url=ISSUER) as client:
        rejected = _register(client, redirect_uri, expected_status=400)
        assert rejected["error"] == "invalid_redirect_uri"


def test_one_client_has_one_pending_state_and_one_failure_budget(monkeypatch, tmp_path):
    _oauth_env(monkeypatch, tmp_path / "state.json")
    app = build_server(REGISTRY).streamable_http_app(
        stateless_http=True, json_response=True
    )
    with TestClient(app, base_url=ISSUER) as client:
        registration = _register(client)
        old_state, _ = _authorize(client, registration)
        new_state, _ = _authorize(client, registration)

        assert client.get("/login", params={"state": old_state}).status_code == 400
        assert client.get("/login", params={"state": new_state}).status_code == 200

        for _ in range(5):
            response = client.post(
                "/login",
                data={"state": new_state, "password": "wrong-password"},
                follow_redirects=False,
            )
            assert response.status_code == 401

        rotated_state, _ = _authorize(client, registration)
        throttled = client.post(
            "/login",
            data={"state": rotated_state, "password": "wrong-password"},
            follow_redirects=False,
        )
        assert throttled.status_code == 429

        approved = client.post(
            "/login",
            data={"state": rotated_state, "password": PASSWORD},
            follow_redirects=False,
        )
        assert approved.status_code == 302


def test_authorization_enforces_resource_and_pkce_code_is_single_use(
    monkeypatch, tmp_path
):
    _oauth_env(monkeypatch, tmp_path / "state.json")
    app = build_server(REGISTRY).streamable_http_app(
        stateless_http=True, json_response=True
    )
    with TestClient(app, base_url=ISSUER) as client:
        registration = _register(client)
        wrong_resource = client.get(
            "/authorize",
            params={
                "client_id": registration["client_id"],
                "redirect_uri": REDIRECT_URI,
                "response_type": "code",
                "code_challenge": "challenge",
                "code_challenge_method": "S256",
                "scope": "hermes",
                "resource": "https://attacker.example/mcp",
            },
            follow_redirects=False,
        )
        assert wrong_resource.status_code == 302
        assert parse_qs(urlparse(wrong_resource.headers["location"]).query)[
            "error"
        ] == ["invalid_target"]

        login_state, verifier = _authorize(client, registration)
        approved = client.post(
            "/login",
            data={"state": login_state, "password": PASSWORD},
            follow_redirects=False,
        )
        code = parse_qs(urlparse(approved.headers["location"]).query)["code"][0]
        token_data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": registration["client_id"],
            "client_secret": registration["client_secret"],
            "resource": f"{ISSUER}/mcp",
        }
        bad_pkce = client.post(
            "/token", data={**token_data, "code_verifier": "incorrect-verifier"}
        )
        assert bad_pkce.status_code == 400
        assert bad_pkce.json()["error"] == "invalid_grant"

        issued = client.post("/token", data={**token_data, "code_verifier": verifier})
        assert issued.status_code == 200
        replayed = client.post("/token", data={**token_data, "code_verifier": verifier})
        assert replayed.status_code == 400
        assert replayed.json()["error"] == "invalid_grant"


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


def test_oauth_mode_refuses_an_insecure_existing_state_file(monkeypatch, tmp_path):
    state_path = tmp_path / "state.json"
    state_path.write_text(
        '{"clients":{},"access_tokens":{},"refresh_tokens":{}}', encoding="utf-8"
    )
    state_path.chmod(0o644)
    _oauth_env(monkeypatch, state_path)

    with pytest.raises(OAuthConfigError):
        build_server(REGISTRY)


@pytest.mark.parametrize(
    "issuer",
    [
        "https://hermes.example.com/nested",
        "https://hermes.example.com?tenant=one",
        "https://user:password@hermes.example.com",
    ],
)
def test_oauth_mode_refuses_issuer_parts_that_do_not_match_root_routes(
    monkeypatch, tmp_path, issuer
):
    monkeypatch.delenv("HERMES_MCP_KEY", raising=False)
    monkeypatch.setenv("HERMES_MCP_OAUTH_ISSUER", issuer)
    monkeypatch.setenv("HERMES_MCP_OAUTH_PASSWORD", PASSWORD)
    monkeypatch.setenv("HERMES_MCP_OAUTH_STATE_PATH", str(tmp_path / "state.json"))

    with pytest.raises(OAuthConfigError):
        build_server(REGISTRY)
