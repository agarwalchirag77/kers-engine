"""Engine-side filter for incoming webhook events.

Filtering rules live in `config/filters.json` and are read once at startup.
Filtering decisions are made at the webhook layer — filtered events get a
200 response (Zendesk should not retry; we just chose not to act on the
event) and a structured `eval_skipped_filtered` log entry with the reason.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from .models import MessageEvent


CONNECTORS_DIR = Path(__file__).parent / "connectors"
FILTERS_PATH = CONNECTORS_DIR / "zendesk" / "filters.json"


class FilterConfig(BaseModel):
    """Whitelist of allowed groups + blacklist of tag values.

    `allowed_group_ids` matches against MessageEvent.group_id (which holds
    whatever the Zendesk trigger sends — names or numeric IDs). Empty list =
    accept all groups.

    `excluded_tags` is a flat list of tag strings. If any tag on the incoming
    event matches, the event is filtered out.
    """

    allowed_group_ids: list[str] = Field(default_factory=list)
    excluded_tags: list[str] = Field(default_factory=list)


def load_filter_config(path: Path = FILTERS_PATH) -> FilterConfig:
    if not path.exists():
        return FilterConfig()  # default: accept everything
    with path.open() as f:
        data = json.load(f)
    return FilterConfig.model_validate(data)


def should_process(
    event: MessageEvent,
    config: FilterConfig,
    *,
    check_group: bool = True,
) -> tuple[bool, Optional[str]]:
    """Decide whether an inbound event should hit the worker.

    Returns (True, None) if the event passes the filter.
    Returns (False, reason) if the event should be dropped.

    `check_group=False` skips the group whitelist check. Used for messaging
    (chat) events, which by product policy are processed regardless of which
    group the ticket sits in — the only thing that can stop ERS on a chat
    ticket is an excluded tag (e.g., spam_spam).
    """
    if check_group and config.allowed_group_ids:
        if event.group_id not in config.allowed_group_ids:
            return False, f"group '{event.group_id}' not in allowed_group_ids"

    if config.excluded_tags:
        overlap = set(event.tags) & set(config.excluded_tags)
        if overlap:
            return False, f"excluded tag(s): {sorted(overlap)}"

    return True, None
