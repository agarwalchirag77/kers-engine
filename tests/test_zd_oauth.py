from __future__ import annotations

import httpx
import pytest

from app.connectors.zendesk.oauth import (
    ZendeskOAuth,
    ZendeskOAuthError,
    token_url_for,
)


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)


def _make(handler, **kw):
    return ZendeskOAuth(
        token_url=token_url_for("test"),
        client_id="kers_engine",
        client_secret="secret",
        scope="tickets:write tickets:read",
        client=_client(handler),
        **kw,
    )


async def test_fetches_and_caches():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert str(request.url).endswith("/oauth/tokens")
        body = request.read().decode()
        assert '"grant_type": "client_credentials"' in body or '"grant_type":"client_credentials"' in body
        return httpx.Response(200, json={"access_token": "abc", "expires_in": 1800})

    oauth = _make(handler)
    t1 = await oauth.get_token()
    t2 = await oauth.get_token()
    assert t1 == t2 == "abc"
    assert len(calls) == 1, "second call should be served from cache"


async def test_force_refresh_refetches():
    seq = ["first", "second"]
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"access_token": seq[len(calls) - 1], "expires_in": 1800})

    oauth = _make(handler)
    assert await oauth.get_token() == "first"
    assert await oauth.get_token(force_refresh=True) == "second"
    assert len(calls) == 2


async def test_refetches_when_near_expiry():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        # Token already inside the refresh margin: expires_in < margin.
        return httpx.Response(200, json={"access_token": f"t{len(calls)}", "expires_in": 10})

    oauth = _make(handler, refresh_margin_seconds=120.0)
    assert await oauth.get_token() == "t1"
    # Second call sees the cached token as stale (10s < 120s margin) -> refetch.
    assert await oauth.get_token() == "t2"
    assert len(calls) == 2


async def test_raises_on_non_200():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="invalid_client")

    oauth = _make(handler)
    with pytest.raises(ZendeskOAuthError) as ei:
        await oauth.get_token()
    assert "401" in str(ei.value)


async def test_raises_on_missing_access_token():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"token_type": "bearer"})

    oauth = _make(handler)
    with pytest.raises(ZendeskOAuthError):
        await oauth.get_token()
