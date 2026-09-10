from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..models import PushResult, SentimentResponse, TicketMetadata


SCHEMA = """
CREATE TABLE IF NOT EXISTS ers_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id TEXT NOT NULL,
    evaluated_at TEXT NOT NULL,
    ers REAL NOT NULL,
    sentiment_breakdown TEXT NOT NULL,
    top_signal TEXT NOT NULL,
    confidence REAL NOT NULL,
    ai_reasoning TEXT NOT NULL,
    commitments_detected INTEGER NOT NULL,
    commitments_missed INTEGER NOT NULL,
    message_count INTEGER NOT NULL,
    previous_ers REAL,
    delta REAL,
    threshold REAL NOT NULL,
    threshold_breached INTEGER NOT NULL,
    pushed_to_zd INTEGER NOT NULL,
    pushed_to_zd_at TEXT,
    push_error TEXT,
    agent_id TEXT,
    group_id TEXT,
    customer_tier TEXT,
    ticket_priority TEXT,
    model_version TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    false_positive INTEGER,
    resolution_outcome TEXT
);

CREATE INDEX IF NOT EXISTS ix_ers_events_ticket_id ON ers_events(ticket_id);
CREATE INDEX IF NOT EXISTS ix_ers_events_evaluated_at ON ers_events(evaluated_at);

-- One-row-per-ticket summary view. Backed by the append-only ers_events
-- table; gives the "latest state + lifetime stats" view without duplicating
-- storage. Use this for casual browsing; use ers_events for full history.
CREATE VIEW IF NOT EXISTS tickets_summary AS
SELECT
  ticket_id,
  COUNT(*) AS evals,
  ROUND(MIN(ers), 2) AS min_ers,
  ROUND(MAX(ers), 2) AS max_ers,
  ROUND(AVG(ers), 2) AS avg_ers,
  ROUND((SELECT ers FROM ers_events e2
         WHERE e2.ticket_id = e.ticket_id
         ORDER BY id DESC LIMIT 1), 2) AS latest_ers,
  (SELECT top_signal FROM ers_events e2
   WHERE e2.ticket_id = e.ticket_id
   ORDER BY id DESC LIMIT 1) AS latest_top_signal,
  MAX(commitments_detected) AS max_commits_detected,
  MAX(commitments_missed) AS max_commits_missed,
  (SELECT resolution_outcome FROM ers_events e2
   WHERE e2.ticket_id = e.ticket_id AND resolution_outcome IS NOT NULL
   LIMIT 1) AS resolution,
  MIN(evaluated_at) AS first_evaluated_at,
  MAX(evaluated_at) AS last_evaluated_at
FROM ers_events e
GROUP BY ticket_id;
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def latest_ers_for_ticket(self, ticket_id: str) -> Optional[float]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT ers FROM ers_events WHERE ticket_id = ? "
                "ORDER BY evaluated_at DESC LIMIT 1",
                (ticket_id,),
            ).fetchone()
        return row["ers"] if row else None

    def insert_ers_event(
        self,
        *,
        ticket_id: str,
        ers: float,
        sentiment: SentimentResponse,
        message_count: int,
        threshold: float,
        push_result: PushResult,
        metadata: TicketMetadata,
        model_version: str,
        prompt_version: str,
        evaluated_at: Optional[datetime] = None,
    ) -> int:
        """`evaluated_at` defaults to now. Pass it explicitly only when
        replaying history (e.g. scripts/replay_gap.py after an outage), so the
        row is stamped with the moment the message actually arrived rather than
        the moment we caught up."""
        previous_ers = self.latest_ers_for_ticket(ticket_id)
        delta = (ers - previous_ers) if previous_ers is not None else None
        breached = 1 if ers >= threshold else 0
        now = (evaluated_at or datetime.now(timezone.utc)).isoformat()

        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO ers_events (
                    ticket_id, evaluated_at, ers, sentiment_breakdown, top_signal,
                    confidence, ai_reasoning, commitments_detected,
                    commitments_missed, message_count, previous_ers, delta,
                    threshold, threshold_breached, pushed_to_zd, pushed_to_zd_at,
                    push_error, agent_id, group_id, ticket_priority,
                    model_version, prompt_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ticket_id,
                    now,
                    ers,
                    json.dumps(sentiment.breakdown.as_dict()),
                    sentiment.top_signal,
                    sentiment.confidence,
                    sentiment.reasoning,
                    sentiment.commitments_detected,
                    sentiment.commitments_missed,
                    message_count,
                    previous_ers,
                    delta,
                    threshold,
                    breached,
                    1 if push_result.success else 0,
                    push_result.pushed_at.isoformat() if push_result.pushed_at else None,
                    push_result.error,
                    metadata.agent_email or None,
                    metadata.group_id or None,
                    metadata.priority or None,
                    model_version,
                    prompt_version,
                ),
            )
            return cur.lastrowid

    def update_resolution(self, ticket_id: str, outcome: Optional[str]) -> int:
        """Backfill resolution_outcome on all rows for a ticket when it closes."""
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE ers_events SET resolution_outcome = ? "
                "WHERE ticket_id = ? AND resolution_outcome IS NULL",
                (outcome, ticket_id),
            )
            return cur.rowcount

    def evaluation_count(self, ticket_id: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM ers_events WHERE ticket_id = ?",
                (ticket_id,),
            ).fetchone()
        return row["n"]

    def latest_evaluated_message_count(self, ticket_id: str) -> int:
        """Largest message_count we've ever evaluated for this ticket.

        Used by the backfill script to skip tickets that are already up to
        date (no new messages since the last eval) while still re-running
        tickets that have grown since.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT MAX(message_count) AS n FROM ers_events WHERE ticket_id = ?",
                (ticket_id,),
            ).fetchone()
        return (row["n"] or 0) if row else 0
