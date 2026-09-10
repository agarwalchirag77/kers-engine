from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


class StructuredLogger:
    """JSONL structured logger. One file per day; appends are atomic under
    PIPE_BUF for our line sizes."""

    def __init__(self, log_dir: Path):
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)

    def _path(self) -> Path:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return self.log_dir / f"engine-{date_str}.jsonl"

    def _emit(
        self,
        level: str,
        stage: str,
        ticket_id: Optional[str] = None,
        message: str = "",
        duration_ms: Optional[float] = None,
        error: Optional[str] = None,
        extra: Optional[dict[str, Any]] = None,
    ) -> None:
        record: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": level,
            "stage": stage,
            "message": message,
        }
        if ticket_id is not None:
            record["ticket_id"] = ticket_id
        if duration_ms is not None:
            record["duration_ms"] = duration_ms
        if error is not None:
            record["error"] = error
        if extra:
            record["extra"] = extra

        path = self._path()
        # If today's file doesn't exist yet, this write will create it. That
        # marks a fresh UTC day — take the opportunity to delete every older
        # engine-*.jsonl file so we keep only the current day's log on disk.
        is_new_day_file = not path.exists()
        with path.open("a") as f:
            f.write(json.dumps(record, default=str) + "\n")
        if is_new_day_file:
            self._purge_stale_log_files(current=path)

    def _purge_stale_log_files(self, current: Path) -> None:
        """Delete every engine-*.jsonl in the log dir except `current`.

        Called only on the first write of a new UTC day. Errors deleting
        individual files (permissions, race with another process) are
        swallowed — a logger's cleanup pass must never crash the caller.
        """
        try:
            for f in self.log_dir.glob("engine-*.jsonl"):
                if f == current:
                    continue
                try:
                    f.unlink()
                except OSError:
                    pass
        except OSError:
            pass

    def info(self, stage: str, **kwargs: Any) -> None:
        self._emit("INFO", stage, **kwargs)

    def warn(self, stage: str, **kwargs: Any) -> None:
        self._emit("WARN", stage, **kwargs)

    def error(self, stage: str, **kwargs: Any) -> None:
        self._emit("ERROR", stage, **kwargs)
