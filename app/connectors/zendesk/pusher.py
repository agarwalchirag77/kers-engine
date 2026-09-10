from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional

import httpx

from ...models import PushResult


class ZendeskPusherHTTP:
    """v1 ZendeskPusher: PUTs the ERS custom field via the Zendesk Tickets API.

    Retry policy (per plan EH4/EH5):
    - 4xx: do not retry; capture error.
    - 5xx or transport error: retry up to 3 times with backoff (1s, 2s, 4s).
    """

    def __init__(
        self,
        subdomain: str,
        api_user: str,
        api_token: str,
        custom_field_id: int,
        client: Optional[httpx.AsyncClient] = None,
        timeout_seconds: float = 15.0,
    ):
        self.url_template = (
            f"https://{subdomain}.zendesk.com/api/v2/tickets/{{ticket_id}}.json"
        )
        self.auth = (f"{api_user}/token", api_token)
        self.custom_field_id = custom_field_id
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(timeout=timeout_seconds)

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

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

        for attempt in range(4):  # 1 initial + 3 retries
            try:
                resp = await self.client.put(url, json=body, auth=self.auth)
                if 200 <= resp.status_code < 300:
                    return PushResult(
                        success=True,
                        pushed_at=datetime.now(timezone.utc),
                    )
                if 400 <= resp.status_code < 500:
                    return PushResult(
                        success=False,
                        error=f"HTTP {resp.status_code}: {resp.text[:200]}",
                    )
                last_error = f"HTTP {resp.status_code}"
            except httpx.RequestError as e:
                last_error = f"transport: {e}"

            if attempt < 3:
                await asyncio.sleep(backoffs[attempt])

        return PushResult(success=False, error=f"After 3 retries: {last_error}")
