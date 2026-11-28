"""Zendesk OAuth client-credentials token provider.

Zendesk is retiring API-token (Basic `{email}/token:{token}`) auth; the
supported path is an OAuth client. This module fetches a short-lived bearer
access token via the client-credentials grant:

    POST https://{subdomain}.zendesk.com/oauth/tokens
    Content-Type: application/json
    {"grant_type": "client_credentials", "client_id": ..., "client_secret": ...,
     "scope": "tickets:write tickets:read"}
    -> {"access_token": "...", "token_type": "bearer", "expires_in": 1800, ...}

Tokens are short-lived (expires_in ~1800s), so we cache in memory and refresh
proactively before expiry. Callers force a refresh after a 401.

Two variants with identical semantics: `ZendeskOAuth` (async, for the engine)
and `ZendeskOAuthSync` (for the one-shot operational scripts).
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
from typing import Optional

import httpx


class ZendeskOAuthError(Exception):
    pass


def token_url_for(subdomain: str) -> str:
    return f"https://{subdomain}.zendesk.com/oauth/tokens"


def _auth_params_from_config(zd_config: dict) -> dict:
    """Build the OAuth constructor kwargs from the zendesk settings dict + env.

    Raises ZendeskOAuthError if the client id/secret env vars are unset, so
    callers (engine startup, scripts) get one clear message instead of a later
    401.
    """
    auth = zd_config.get("auth", {})
    id_env = auth.get("client_id_env", "ZENDESK_OAUTH_CLIENT_ID")
    secret_env = auth.get("client_secret_env", "ZENDESK_OAUTH_CLIENT_SECRET")
    client_id = os.environ.get(id_env, "")
    client_secret = os.environ.get(secret_env, "")
    if not client_id or not client_secret:
        raise ZendeskOAuthError(
            f"Zendesk OAuth credentials not set (expected env {id_env} and {secret_env})"
        )
    return {
        "token_url": token_url_for(zd_config["subdomain"]),
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": auth.get("scope", "tickets:write tickets:read"),
        "grant_type": auth.get("grant_type", "client_credentials"),
    }


def oauth_from_config(zd_config: dict, **kw) -> "ZendeskOAuth":
    return ZendeskOAuth(**_auth_params_from_config(zd_config), **kw)


def oauth_sync_from_config(zd_config: dict, **kw) -> "ZendeskOAuthSync":
    return ZendeskOAuthSync(**_auth_params_from_config(zd_config), **kw)


def _request_body(client_id: str, client_secret: str, scope: str, grant_type: str) -> dict:
    return {
        "grant_type": grant_type,
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": scope,
    }


def _extract_token(status_code: int, text: str, json_fn) -> tuple[str, float]:
    """Validate a token response and return (access_token, ttl_seconds)."""
    if status_code != 200:
        raise ZendeskOAuthError(f"token request failed: HTTP {status_code}: {text[:200]}")
    try:
        data = json_fn()
    except ValueError as e:
        raise ZendeskOAuthError(f"token response was not JSON: {e}") from e
    token = data.get("access_token")
    if not token:
        raise ZendeskOAuthError("token response missing access_token")
    expires_in = data.get("expires_in")
    # Conservative fallback if the server omits expires_in.
    ttl = float(expires_in) if isinstance(expires_in, (int, float)) else 1500.0
    return token, ttl


class ZendeskOAuth:
    """Async client-credentials token provider with in-memory caching."""

    def __init__(
        self,
        *,
        token_url: str,
        client_id: str,
        client_secret: str,
        scope: str,
        grant_type: str = "client_credentials",
        client: Optional[httpx.AsyncClient] = None,
        timeout_seconds: float = 15.0,
        refresh_margin_seconds: float = 120.0,
    ):
        self.token_url = token_url
        self._body = _request_body(client_id, client_secret, scope, grant_type)
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(timeout=timeout_seconds)
        self._refresh_margin = refresh_margin_seconds
        self._token: Optional[str] = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    def _is_fresh(self) -> bool:
        return (
            self._token is not None
            and (self._expires_at - time.monotonic()) > self._refresh_margin
        )

    async def get_token(self, *, force_refresh: bool = False) -> str:
        if not force_refresh and self._is_fresh():
            return self._token  # type: ignore[return-value]
        async with self._lock:
            # Another coroutine may have refreshed while we waited for the lock.
            if not force_refresh and self._is_fresh():
                return self._token  # type: ignore[return-value]
            try:
                resp = await self.client.post(
                    self.token_url,
                    json=self._body,
                    headers={"Content-Type": "application/json"},
                )
            except httpx.RequestError as e:
                raise ZendeskOAuthError(f"token request transport error: {e}") from e
            token, ttl = _extract_token(resp.status_code, resp.text, resp.json)
            self._token = token
            self._expires_at = time.monotonic() + ttl
            return token


class ZendeskOAuthSync:
    """Synchronous twin of ZendeskOAuth for the one-shot operational scripts.

    Scripts can run longer than a token's 30-minute life, so they must call
    get_token() per request (it refreshes transparently) and force_refresh on a
    401, exactly like the engine.
    """

    def __init__(
        self,
        *,
        token_url: str,
        client_id: str,
        client_secret: str,
        scope: str,
        grant_type: str = "client_credentials",
        timeout_seconds: float = 15.0,
        refresh_margin_seconds: float = 120.0,
    ):
        self.token_url = token_url
        self._body = _request_body(client_id, client_secret, scope, grant_type)
        self._timeout = timeout_seconds
        self._refresh_margin = refresh_margin_seconds
        self._token: Optional[str] = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    def _is_fresh(self) -> bool:
        return (
            self._token is not None
            and (self._expires_at - time.monotonic()) > self._refresh_margin
        )

    def get_token(self, *, force_refresh: bool = False) -> str:
        if not force_refresh and self._is_fresh():
            return self._token  # type: ignore[return-value]
        with self._lock:
            if not force_refresh and self._is_fresh():
                return self._token  # type: ignore[return-value]
            try:
                resp = httpx.post(
                    self.token_url,
                    json=self._body,
                    headers={"Content-Type": "application/json"},
                    timeout=self._timeout,
                )
            except httpx.RequestError as e:
                raise ZendeskOAuthError(f"token request transport error: {e}") from e
            token, ttl = _extract_token(resp.status_code, resp.text, resp.json)
            self._token = token
            self._expires_at = time.monotonic() + ttl
            return token

    def auth_header(self, *, force_refresh: bool = False) -> dict:
        return {"Authorization": f"Bearer {self.get_token(force_refresh=force_refresh)}"}
