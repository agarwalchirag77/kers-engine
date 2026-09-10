from __future__ import annotations

import httpx
import pytest

from app.connectors.zendesk.pusher import ZendeskPusherHTTP


async def _build_pusher(handler):
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, timeout=5.0)
    return ZendeskPusherHTTP(
        subdomain="test",
        api_user="user@x.com",
        api_token="token",
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
    # ERS should be sent as the custom field value, formatted to 2 decimals
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


async def test_retry_on_5xx_then_success():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) < 3:
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(200, json={})

    # Speed up the test by patching asyncio.sleep before construction is fine;
    # easier: rely on the fact that backoffs are 1s, 2s, 4s — total ~3s for 3rd try.
    # We'll just monkey-patch asyncio.sleep inside the pusher's module scope.
    import asyncio as _asyncio
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
