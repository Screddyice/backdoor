"""Single-owner OAuth provider for browser-based remote MCP clients.

Claude's web connector performs OAuth authorization-code flow with PKCE. The
bridge has one human owner, so it uses a password consent screen instead of an
external identity provider. Dynamic client registrations and issued tokens are
stored in a mode-600 JSON file so a service restart does not disconnect Claude.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import secrets
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

MIN_PASSWORD_LEN = 16
DEFAULT_SCOPE = "hermes"
ACCESS_TOKEN_TTL = 3600
REFRESH_TOKEN_TTL = 30 * 24 * 3600
LOGIN_FAILURE_LIMIT = 5
LOGIN_FAILURE_WINDOW = 300


class OAuthConfigError(RuntimeError):
    """OAuth mode is enabled but its deployment settings are unsafe."""


@dataclass(frozen=True)
class OAuthSettings:
    issuer: AnyHttpUrl
    resource_url: AnyHttpUrl
    password_digest: bytes
    state_path: Path
    scope: str = DEFAULT_SCOPE

    @classmethod
    def from_env(cls) -> OAuthSettings:
        issuer_raw = os.environ.get("HERMES_MCP_OAUTH_ISSUER", "").strip().rstrip("/")
        if not issuer_raw:
            raise OAuthConfigError("HERMES_MCP_OAUTH_ISSUER is unset")
        try:
            issuer = AnyHttpUrl(issuer_raw)
            resource_url = AnyHttpUrl(f"{issuer_raw}/mcp")
        except ValueError as exc:
            raise OAuthConfigError(
                "HERMES_MCP_OAUTH_ISSUER is not a valid HTTP URL"
            ) from exc
        if issuer.scheme != "https" and issuer.host not in {
            "localhost",
            "127.0.0.1",
            "[::1]",
        }:
            raise OAuthConfigError(
                "HERMES_MCP_OAUTH_ISSUER must use HTTPS outside loopback"
            )

        password = os.environ.get("HERMES_MCP_OAUTH_PASSWORD", "")
        if len(password) < MIN_PASSWORD_LEN or len(set(password)) <= 2:
            raise OAuthConfigError(
                f"HERMES_MCP_OAUTH_PASSWORD must be at least {MIN_PASSWORD_LEN} "
                "characters and must not be placeholder-shaped"
            )

        path_raw = os.environ.get("HERMES_MCP_OAUTH_STATE_PATH", "").strip()
        state_path = (
            Path(path_raw).expanduser()
            if path_raw
            else Path.home() / ".config" / "hermes-mcp" / "oauth-state.json"
        )
        return cls(
            issuer=issuer,
            resource_url=resource_url,
            password_digest=hashlib.sha256(password.encode("utf-8")).digest(),
            state_path=state_path,
        )


class SingleUserOAuthProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    """OAuth authorization server gated by one deployment password."""

    def __init__(self, settings: OAuthSettings) -> None:
        self.settings = settings
        self.clients: dict[str, OAuthClientInformationFull] = {}
        self.authorization_codes: dict[str, AuthorizationCode] = {}
        self.access_tokens: dict[str, AccessToken] = {}
        self.refresh_tokens: dict[str, RefreshToken] = {}
        self.pending: dict[str, tuple[str, AuthorizationParams]] = {}
        self.failed_logins: deque[float] = deque()
        self._load()

    def _load(self) -> None:
        path = self.settings.state_path
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self.clients = {
                key: OAuthClientInformationFull.model_validate(value)
                for key, value in data.get("clients", {}).items()
            }
            self.access_tokens = {
                key: AccessToken.model_validate(value)
                for key, value in data.get("access_tokens", {}).items()
            }
            self.refresh_tokens = {
                key: RefreshToken.model_validate(value)
                for key, value in data.get("refresh_tokens", {}).items()
            }
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise OAuthConfigError(
                f"OAuth state at {path} is unreadable or invalid"
            ) from exc

    def _save(self) -> None:
        path = self.settings.state_path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            path.parent.chmod(0o700)
        except OSError:
            pass
        payload = {
            "clients": {
                key: value.model_dump(mode="json")
                for key, value in self.clients.items()
            },
            "access_tokens": {
                key: value.model_dump(mode="json")
                for key, value in self.access_tokens.items()
            },
            "refresh_tokens": {
                key: value.model_dump(mode="json")
                for key, value in self.refresh_tokens.items()
            },
        }
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            path.chmod(0o600)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self.clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self.clients[client_info.client_id] = client_info
        self._save()

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        login_state = secrets.token_urlsafe(32)
        self.pending[login_state] = (client.client_id, params)
        return f"{str(self.settings.issuer).rstrip('/')}/login?state={login_state}"

    async def handle_login(self, request: Request) -> Response:
        if request.method == "GET":
            login_state = request.query_params.get("state", "")
            if login_state not in self.pending:
                return HTMLResponse(
                    "Invalid or expired authorization request", status_code=400
                )
            escaped_state = html.escape(login_state, quote=True)
            return HTMLResponse(
                "<!doctype html><html><head><title>Screddy Hermes</title></head>"
                "<body><main><h1>Connect Screddy Hermes</h1>"
                "<p>Enter the connector password to authorize Claude.</p>"
                f'<form method="post" action="/login">'
                f'<input type="hidden" name="state" value="{escaped_state}">'
                '<label>Password <input type="password" name="password" '
                'autocomplete="current-password" required></label>'
                '<button type="submit">Connect</button></form></main></body></html>'
            )

        form = await request.form()
        login_state = form.get("state")
        password = form.get("password")
        if not isinstance(login_state, str) or login_state not in self.pending:
            return HTMLResponse(
                "Invalid or expired authorization request", status_code=400
            )
        if not isinstance(password, str):
            return HTMLResponse("Invalid credentials", status_code=401)
        now = time.monotonic()
        while (
            self.failed_logins and self.failed_logins[0] <= now - LOGIN_FAILURE_WINDOW
        ):
            self.failed_logins.popleft()
        if len(self.failed_logins) >= LOGIN_FAILURE_LIMIT:
            return HTMLResponse(
                "Too many failed attempts. Try again later.",
                status_code=429,
                headers={"Retry-After": str(LOGIN_FAILURE_WINDOW)},
            )
        supplied = hashlib.sha256(password.encode("utf-8")).digest()
        if not hmac.compare_digest(supplied, self.settings.password_digest):
            self.failed_logins.append(now)
            return HTMLResponse("Invalid credentials", status_code=401)
        self.failed_logins.clear()

        client_id, params = self.pending.pop(login_state)
        code_value = f"mcp_{secrets.token_urlsafe(32)}"
        code = AuthorizationCode(
            code=code_value,
            scopes=params.scopes or [self.settings.scope],
            expires_at=time.time() + 300,
            client_id=client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
            subject="shawn",
        )
        self.authorization_codes[code_value] = code
        target = construct_redirect_uri(
            str(params.redirect_uri), code=code_value, state=params.state
        )
        return RedirectResponse(target, status_code=302)

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        code = self.authorization_codes.get(authorization_code)
        return code if code and code.client_id == client.client_id else None

    def _issue_tokens(
        self, client_id: str, scopes: list[str], resource: str | None
    ) -> OAuthToken:
        now = int(time.time())
        access_value = f"mcp_at_{secrets.token_urlsafe(48)}"
        refresh_value = f"mcp_rt_{secrets.token_urlsafe(48)}"
        self.access_tokens[access_value] = AccessToken(
            token=access_value,
            client_id=client_id,
            scopes=scopes,
            expires_at=now + ACCESS_TOKEN_TTL,
            resource=resource,
            subject="shawn",
        )
        self.refresh_tokens[refresh_value] = RefreshToken(
            token=refresh_value,
            client_id=client_id,
            scopes=scopes,
            expires_at=now + REFRESH_TOKEN_TTL,
            subject="shawn",
        )
        self._save()
        return OAuthToken(
            access_token=access_value,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL,
            scope=" ".join(scopes),
            refresh_token=refresh_value,
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        stored = self.authorization_codes.pop(authorization_code.code, None)
        if stored is None or stored.client_id != client.client_id:
            raise TokenError(
                error="invalid_grant", error_description="authorization code is invalid"
            )
        return self._issue_tokens(client.client_id, stored.scopes, stored.resource)

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        token = self.refresh_tokens.get(refresh_token)
        if token is None or token.client_id != client.client_id:
            return None
        if token.expires_at and token.expires_at < int(time.time()):
            self.refresh_tokens.pop(refresh_token, None)
            self._save()
            return None
        return token

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        stored = self.refresh_tokens.pop(refresh_token.token, None)
        if stored is None or stored.client_id != client.client_id:
            raise TokenError(
                error="invalid_grant", error_description="refresh token is invalid"
            )
        return self._issue_tokens(client.client_id, scopes, None)

    async def load_access_token(self, token: str) -> AccessToken | None:
        access = self.access_tokens.get(token)
        if access is None:
            return None
        if access.expires_at and access.expires_at < int(time.time()):
            self.access_tokens.pop(token, None)
            self._save()
            return None
        return access

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        self.access_tokens.pop(token.token, None)
        self.refresh_tokens.pop(token.token, None)
        self._save()
