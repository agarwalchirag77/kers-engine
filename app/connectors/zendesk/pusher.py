from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional

import httpx

from ...models import PushResult
from .oauth import ZendeskOAuth, ZendeskOAuthError


class ZendeskPusherHTTP:
    """ZendeskPusher: PUTs the ERS custom field via the Zendesk Tickets API,
    authenticated with an OAuth bearer token (Zendesk is retiring API tokens).

    Retry policy (per plan EH4/EH5):
    - 401: the token may have expired/rotated early — force one token refresh
      and retry once (does not count against the 5xx retry budget).
    - other 4xx: do not retry; capture error.
    - 5xx or transport error: retry up to 3 times with backoff (1s, 2s, 4s).
    """

    def __init__(
        self,
        subdomain: str,
        oauth: ZendeskOAuth,
        custom_field_id: int,
        client: Optional[httpx.AsyncClient] = None,
        timeout_seconds: float = 15.0,
    ):
        self.url_template = (
            f"https://{subdomain}.zendesk.com/api/v2/tickets/{{ticket_id}}.json"
        )
        self.oauth = oauth
        self.custom_field_id = custom_field_id
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(timeout=timeout_seconds)

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()
        await self.oauth.close()

    async def _auth_header(self, *, force_refresh: bool = False) -> dict:
        token = await self.oauth.get_token(force_refresh=force_refresh)
        return {"Authorization": f"Bearer {token}"}

    async def push_ers(self, ticket_id: str, ers: float) -> PushResult:
        url = self.url_template.format(ticket_id=ticket_id)
        body = {
            "ticket": {
                "custom_fields": [
                    {"id": self.custom_field_id, "value": f"{ers:.2f}"}
                ]
            }
        }

        last_error: Optional[str] = None
        backoffs = [1.0, 2.0, 4.0]
        retried_auth = False

        attempt = 0
        while attempt < 4:  # 1 initial + 3 retries (for 5xx/transport)
            try:
                headers = await self._auth_header()
            except ZendeskOAuthError as e:
                return PushResult(success=False, error=f"oauth: {e}")

            try:
                resp = await self.client.put(url, json=body, headers=headers)
                if 200 <= resp.status_code < 300:
                    return PushResult(
                        success=True,
                        pushed_at=datetime.now(timezone.utc),
                    )
                if resp.status_code == 401 and not retried_auth:
                    # Token likely expired/revoked early. Force a refresh and
                    # retry once without consuming the 5xx retry budget.
                    retried_auth = True
                    try:
                        await self.oauth.get_token(force_refresh=True)
                    except ZendeskOAuthError as e:
                        return PushResult(success=False, error=f"oauth refresh: {e}")
                    continue
                if 400 <= resp.status_code < 500:
                    return PushResult(
                        success=False,
                        error=f"HTTP {resp.status_code}: {resp.text[:200]}",
                    )
                last_error = f"HTTP {resp.status_code}"
            except httpx.RequestError as e:
                last_error = f"transport: {e}"

            attempt += 1
            if attempt < 4:
                await asyncio.sleep(backoffs[attempt - 1])

        return PushResult(success=False, error=f"After 3 retries: {last_error}")
