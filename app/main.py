from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from .ai_client import OpenAIClient
from .config import (
    DEFAULT_MODEL,
    PROMPT_VERSION,
    load_weight_config,
    load_zendesk_config,
)
from .storage.db import Database
from .storage.logger import StructuredLogger
from .storage.ticket_files import JsonTicketStore
from .webhook import router as webhook_router
from .worker import Worker
from .connectors.zendesk.oauth import ZendeskOAuth, token_url_for
from .connectors.zendesk.pusher import ZendeskPusherHTTP


DATA_DIR = Path(os.environ.get("ERS_DATA_DIR", "./data"))
TICKET_FILES_DIR = Path(os.environ.get("ERS_TICKET_DIR", "./ticket_files"))
LOG_DIR = Path(os.environ.get("ERS_LOG_DIR", "./logs"))
DB_PATH = DATA_DIR / "escalation.sqlite"


def _build_worker() -> Worker:
    weight_config = load_weight_config()
    zd_config = load_zendesk_config()

    logger = StructuredLogger(LOG_DIR)
    ticket_store = JsonTicketStore(TICKET_FILES_DIR)
    db = Database(DB_PATH)

    openai_key = os.environ.get("OPENAI_API_KEY", "")
    if not openai_key:
        logger.warn(stage="missing_env", message="OPENAI_API_KEY not set")
    ai_client = OpenAIClient(
        api_key=openai_key,
        model=os.environ.get("ERS_MODEL", DEFAULT_MODEL),
    )

    auth = zd_config.get("auth", {})
    client_id = os.environ.get(auth.get("client_id_env", "ZENDESK_OAUTH_CLIENT_ID"), "")
    client_secret = os.environ.get(auth.get("client_secret_env", "ZENDESK_OAUTH_CLIENT_SECRET"), "")
    if not client_id or not client_secret:
        logger.warn(stage="missing_env", message="ZENDESK_OAUTH_CLIENT_ID/SECRET not set")
    oauth = ZendeskOAuth(
        token_url=token_url_for(zd_config["subdomain"]),
        client_id=client_id,
        client_secret=client_secret,
        scope=auth.get("scope", "tickets:write tickets:read"),
        grant_type=auth.get("grant_type", "client_credentials"),
    )
    zd_pusher = ZendeskPusherHTTP(
        subdomain=zd_config["subdomain"],
        oauth=oauth,
        custom_field_id=zd_config["ers_custom_field_id"],
    )

    return Worker(
        ticket_store=ticket_store,
        ai_client=ai_client,
        zd_pusher=zd_pusher,
        db=db,
        logger=logger,
        weight_config=weight_config,
        model_version=ai_client.model,
        prompt_version=PROMPT_VERSION,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    worker = _build_worker()
    app.state.worker = worker
    app.state.webhook_secret = os.environ.get(
        load_zendesk_config().get("webhook_signature_env_var", "ZENDESK_WEBHOOK_SECRET")
    )
    worker.start()
    worker.logger.info(stage="engine_started")
    try:
        yield
    finally:
        worker.logger.info(stage="engine_stopping")
        await worker.stop()
        # Close httpx client owned by the pusher
        if hasattr(worker.zd_pusher, "close"):
            await worker.zd_pusher.close()


app = FastAPI(title="Zendesk ERS Engine", lifespan=lifespan)
app.include_router(webhook_router)


@app.get("/health")
async def health():
    return {"status": "ok"}
