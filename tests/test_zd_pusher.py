from __future__ import annotations

import httpx
import pytest

from app.connectors.zendesk.pusher import ZendeskPusherHTTP


class _StubOAuth:
    """Minimal token provider so the pusher's PUT logic is tested in isolation
    from the real token endpoint. Tracks how often a forced refresh happened."""

    def __init__(self, token: str = "tok-abc"):
        self.token = token
        self.get_calls = 0
        self.force_calls = 0

    async def get_token(self, *, force_refresh: bool = False) -> str:
        self.get_calls += 1
        if force_refresh:
            self.force_calls += 1
            self.token = f"{self.token}-r"
        return self.token

    async def close(self) -> None:
        pass


async def _build_pusher(handler, oauth=None):
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, timeout=5.0)
    return ZendeskPusherHTTP(
        subdomain="test",
        oauth=oauth or _StubOAuth(),
        custom_field_id=123,
        client=client,
    )


async def test_success_on_200():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"ticket": {"id": 1}})

    pusher = await _build_pusher(handler)
    result = await pusher.push_ers("t1", 3.6)
    assert result.success is True
    assert result.pushed_at is not None
    assert len(calls) == 1
    # Bearer auth header is sent.
    assert calls[0].headers.get("authorization") == "Bearer tok-abc"
    # ERS should be sent as the custom field value, formatted to 2 decimals.
    body = calls[0].read().decode()
    assert "3.60" in body
    assert '"id":123' in body or '"id": 123' in body


async def test_no_retry_on_4xx():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(400, text="bad request")

    pusher = await _build_pusher(handler)
    result = await pusher.push_ers("t1", 3.6)
    assert result.success is False
    assert "400" in (result.error or "")
    assert len(calls) == 1, "4xx should not retry"


async def test_refresh_on_401_then_success():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(401, text="unauthorized")
        return httpx.Response(200, json={})

    oauth = _StubOAuth()
    pusher = await _build_pusher(handler, oauth=oauth)
    result = await pusher.push_ers("t1", 3.6)
    assert result.success is True
    assert len(calls) == 2, "401 should force a refresh and retry once"
    assert oauth.force_calls == 1
    # The retry used the refreshed token.
    assert calls[1].headers.get("authorization") == "Bearer tok-abc-r"


async def test_401_retries_auth_only_once():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(401, text="unauthorized")

    oauth = _StubOAuth()
    pusher = await _build_pusher(handler, oauth=oauth)
    result = await pusher.push_ers("t1", 3.6)
    assert result.success is False
    assert "401" in (result.error or "")
    assert len(calls) == 2, "one initial + one post-refresh retry, then give up"
    assert oauth.force_calls == 1


async def test_retry_on_5xx_then_success():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) < 3:
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(200, json={})

    import app.connectors.zendesk.pusher as zd_mod

    original_sleep = zd_mod.asyncio.sleep

    async def fast_sleep(_):
        return None

    zd_mod.asyncio.sleep = fast_sleep
    try:
        pusher = await _build_pusher(handler)
        result = await pusher.push_ers("t1", 3.6)
    finally:
        zd_mod.asyncio.sleep = original_sleep

    assert result.success is True
    assert len(calls) == 3


async def test_gives_up_after_3_retries():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(503, text="unavailable")

    import app.connectors.zendesk.pusher as zd_mod

    original_sleep = zd_mod.asyncio.sleep

    async def fast_sleep(_):
        return None

    zd_mod.asyncio.sleep = fast_sleep
    try:
        pusher = await _build_pusher(handler)
        result = await pusher.push_ers("t1", 3.6)
    finally:
        zd_mod.asyncio.sleep = original_sleep

    assert result.success is False
    assert "503" in (result.error or "")
    assert len(calls) == 4, "1 initial + 3 retries = 4 attempts"
