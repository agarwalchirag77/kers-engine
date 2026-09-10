from __future__ import annotations

import time
from pathlib import Path


def purge_old_logs(log_dir: Path, max_age_seconds: int = 24 * 3600) -> list[Path]:
    """Delete JSONL log files older than `max_age_seconds`. Returns the deleted paths."""
    if not log_dir.exists():
        return []
    cutoff = time.time() - max_age_seconds
    deleted: list[Path] = []
    for f in log_dir.glob("*.jsonl"):
        if f.stat().st_mtime < cutoff:
            f.unlink()
            deleted.append(f)
    return deleted
