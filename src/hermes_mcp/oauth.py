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
import stat
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    RegistrationError,
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
AUTHORIZATION_CODE_TTL = 300
PENDING_AUTHORIZATION_TTL = 10 * 60
MAX_PENDING_AUTHORIZATIONS = 100
MAX_REGISTERED_CLIENTS = 32
REGISTRATION_LIMIT = 10
REGISTRATION_WINDOW = 60
LOGIN_FAILURE_LIMIT = 5
LOGIN_FAILURE_WINDOW = 300
OWNER_SUBJECT = "shawn"
LOGIN_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'none'; form-action 'self'; base-uri 'none'; "
        "frame-ancestors 'none'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}


class OAuthConfigError(RuntimeError):
    """OAuth mode is enabled but its deployment settings are unsafe."""


@dataclass(frozen=True)
class OAuthSettings:
    issuer: AnyHttpUrl
    resource_url: AnyHttpUrl
    password_digest: bytes
    state_path: Path
    redirect_hosts: tuple[str, ...]
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
        if (
            issuer.path not in {None, "/"}
            or issuer.query is not None
            or issuer.fragment is not None
            or issuer.username is not None
            or issuer.password is not None
        ):
            raise OAuthConfigError(
                "HERMES_MCP_OAUTH_ISSUER must be an origin without a path, "
                "query, fragment, or credentials"
            )
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

        redirect_hosts = tuple(
            host.strip().lower()
            for host in os.environ.get("HERMES_MCP_OAUTH_REDIRECT_HOSTS", "").split(",")
            if host.strip()
        )
        if not redirect_hosts:
            raise OAuthConfigError("HERMES_MCP_OAUTH_REDIRECT_HOSTS is unset")

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
            redirect_hosts=redirect_hosts,
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
        self.pending: dict[str, tuple[str, AuthorizationParams, float]] = {}
        self.approved_logins: set[str] = set()
        self.failed_logins: deque[float] = deque()
        self.registrations: deque[float] = deque()
        self._load()

    def _load(self) -> None:
        path = self.settings.state_path
        if not path.exists():
            return
        try:
            path_stat = path.lstat()
            if (
                not stat.S_ISREG(path_stat.st_mode)
                or path_stat.st_uid != os.getuid()
                or path_stat.st_mode & 0o077
            ):
                raise OAuthConfigError(
                    f"OAuth state at {path} must be an owner-only regular file"
                )
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
        now = time.monotonic()
        while self.registrations and self.registrations[0] <= now - REGISTRATION_WINDOW:
            self.registrations.popleft()
        if len(self.registrations) >= REGISTRATION_LIMIT:
            raise RegistrationError(
                error="invalid_client_metadata",
                error_description="client registration rate limit reached",
            )
        redirect_uris = client_info.redirect_uris or []
        if not redirect_uris or any(
            (
                uri.scheme != "https"
                and not (
                    uri.scheme == "http"
                    and uri.host in {"localhost", "127.0.0.1", "[::1]"}
                )
            )
            or uri.host is None
            or not any(
                uri.host == allowed or uri.host.endswith(f".{allowed}")
                for allowed in self.settings.redirect_hosts
            )
            for uri in redirect_uris
        ):
            raise RegistrationError(
                error="invalid_redirect_uri",
                error_description="redirect URI host is not approved",
            )
        if len(self.clients) >= MAX_REGISTERED_CLIENTS:
            active_client_ids = {
                token.client_id
                for token in [
                    *self.access_tokens.values(),
                    *self.refresh_tokens.values(),
                ]
            }
            disposable = sorted(
                (
                    client
                    for client in self.clients.values()
                    if client.client_id not in active_client_ids
                ),
                key=lambda client: client.client_id_issued_at or 0,
            )
            if not disposable:
                raise RegistrationError(
                    error="invalid_client_metadata",
                    error_description="client registration capacity reached",
                )
            evicted_client_id = disposable[0].client_id
            self.clients.pop(evicted_client_id, None)
            self.pending = {
                state: item
                for state, item in self.pending.items()
                if item[0] != evicted_client_id
            }
        self.clients[client_info.client_id] = client_info
        self.registrations.append(now)
        self._save()

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        expected_resource = str(self.settings.resource_url)
        if params.resource is not None and params.resource != expected_resource:
            raise AuthorizeError(
                error="invalid_target",
                error_description="requested resource is not this MCP server",
            )
        params.resource = expected_resource
        now = time.monotonic()
        expired = [
            state
            for state, (_, _, created_at) in self.pending.items()
            if created_at <= now - PENDING_AUTHORIZATION_TTL
        ]
        for state in expired:
            self.pending.pop(state, None)
            self.approved_logins.discard(state)
        superseded = [
            state
            for state, (client_id, _, _) in self.pending.items()
            if client_id == client.client_id
        ]
        for state in superseded:
            self.pending.pop(state, None)
            self.approved_logins.discard(state)
        if len(self.pending) >= MAX_PENDING_AUTHORIZATIONS:
            raise AuthorizeError(
                error="temporarily_unavailable",
                error_description="too many pending authorization requests",
            )
        login_state = secrets.token_urlsafe(32)
        self.pending[login_state] = (client.client_id, params, now)
        return f"{str(self.settings.issuer).rstrip('/')}/login?state={login_state}"

    async def handle_login(self, request: Request) -> Response:
        if request.method == "GET":
            login_state = request.query_params.get("state", "")
            if login_state not in self.pending:
                return HTMLResponse(
                    "Invalid or expired authorization request",
                    status_code=400,
                    headers=LOGIN_SECURITY_HEADERS,
                )
            client_id, params, _ = self.pending[login_state]
            client = self.clients.get(client_id)
            client_name = html.escape(
                client.client_name if client and client.client_name else "connector"
            )
            redirect_origin = html.escape(
                f"{params.redirect_uri.scheme}://{params.redirect_uri.host or 'unknown'}"
            )
            escaped_state = html.escape(login_state, quote=True)
            return HTMLResponse(
                "<!doctype html><html><head><title>Screddy Hermes</title></head>"
                "<body><main><h1>Connect Screddy Hermes</h1>"
                f"<p>Authorize {client_name} to return to {redirect_origin}.</p>"
                "<p>Enter the connector password to continue.</p>"
                f'<form method="post" action="/login">'
                f'<input type="hidden" name="state" value="{escaped_state}">'
                '<label>Password <input type="password" name="password" '
                'autocomplete="current-password" required></label>'
                '<button type="submit">Connect</button></form></main></body></html>',
                headers=LOGIN_SECURITY_HEADERS,
            )

        form = await request.form()
        login_state = form.get("state")
        password = form.get("password")
        if not isinstance(login_state, str) or login_state not in self.pending:
            return HTMLResponse(
                "Invalid or expired authorization request",
                status_code=400,
                headers=LOGIN_SECURITY_HEADERS,
            )
        if not isinstance(password, str):
            return HTMLResponse(
                "Invalid credentials", status_code=401, headers=LOGIN_SECURITY_HEADERS
            )
        now = time.monotonic()
        supplied = hashlib.sha256(password.encode("utf-8")).digest()
        if not hmac.compare_digest(supplied, self.settings.password_digest):
            while (
                self.failed_logins
                and self.failed_logins[0] <= now - LOGIN_FAILURE_WINDOW
            ):
                self.failed_logins.popleft()
            if len(self.failed_logins) >= LOGIN_FAILURE_LIMIT:
                return HTMLResponse(
                    "Too many failed attempts. Try again later.",
                    status_code=429,
                    headers={
                        **LOGIN_SECURITY_HEADERS,
                        "Retry-After": str(LOGIN_FAILURE_WINDOW),
                    },
                )
            self.failed_logins.append(now)
            return HTMLResponse(
                "Invalid credentials", status_code=401, headers=LOGIN_SECURITY_HEADERS
            )
        self.failed_logins.clear()
        self.approved_logins.add(login_state)
        completion_target = (
            f"{str(self.settings.issuer).rstrip('/')}/login/complete"
            f"?state={login_state}"
        )
        return RedirectResponse(completion_target, status_code=303)

    async def handle_login_completion(self, request: Request) -> Response:
        login_state = request.query_params.get("state", "")
        if (
            login_state not in self.approved_logins
            or login_state not in self.pending
        ):
            return HTMLResponse(
                "Invalid or expired authorization request",
                status_code=400,
                headers=LOGIN_SECURITY_HEADERS,
            )
        self.approved_logins.discard(login_state)
        client_id, params, _ = self.pending.pop(login_state)
        code_value = f"mcp_{secrets.token_urlsafe(32)}"
        code = AuthorizationCode(
            code=code_value,
            scopes=params.scopes or [self.settings.scope],
            expires_at=time.time() + AUTHORIZATION_CODE_TTL,
            client_id=client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
            subject=OWNER_SUBJECT,
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
        self.access_tokens = {
            key: token
            for key, token in self.access_tokens.items()
            if token.expires_at is None or token.expires_at >= now
        }
        self.refresh_tokens = {
            key: token
            for key, token in self.refresh_tokens.items()
            if token.expires_at is None or token.expires_at >= now
        }
        access_value = f"mcp_at_{secrets.token_urlsafe(48)}"
        refresh_value = f"mcp_rt_{secrets.token_urlsafe(48)}"
        self.access_tokens[access_value] = AccessToken(
            token=access_value,
            client_id=client_id,
            scopes=scopes,
            expires_at=now + ACCESS_TOKEN_TTL,
            resource=resource,
            subject=OWNER_SUBJECT,
        )
        self.refresh_tokens[refresh_value] = RefreshToken(
            token=refresh_value,
            client_id=client_id,
            scopes=scopes,
            expires_at=now + REFRESH_TOKEN_TTL,
            subject=OWNER_SUBJECT,
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
        return self._issue_tokens(
            client.client_id, scopes, str(self.settings.resource_url)
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        access = self.access_tokens.get(token)
        if access is None:
            return None
        if access.resource != str(self.settings.resource_url):
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
