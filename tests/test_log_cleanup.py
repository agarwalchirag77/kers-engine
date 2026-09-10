from __future__ import annotations

import os
import time

from app.storage.log_cleanup import purge_old_logs


def test_purges_old_files_only(tmp_path):
    old = tmp_path / "engine-2026-05-01.jsonl"
    fresh = tmp_path / "engine-2026-06-02.jsonl"
    old.write_text("{}\n")
    fresh.write_text("{}\n")

    # Make `old` older than 24 hours via mtime
    old_time = time.time() - (25 * 3600)
    os.utime(old, (old_time, old_time))

    deleted = purge_old_logs(tmp_path, max_age_seconds=24 * 3600)
    assert old in deleted
    assert fresh not in deleted
    assert not old.exists()
    assert fresh.exists()


def test_handles_missing_dir(tmp_path):
    missing = tmp_path / "nope"
    assert purge_old_logs(missing) == []


# ---- auto-purge on new-day log file creation ----

from datetime import datetime, timezone

from app.storage.logger import StructuredLogger


def _todays_log_name() -> str:
    return f"engine-{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.jsonl"


def test_logger_purges_older_files_when_creating_today(tmp_path):
    """The first write of a new UTC day must delete every older engine-*.jsonl."""
    old1 = tmp_path / "engine-2026-06-01.jsonl"
    old2 = tmp_path / "engine-2026-06-15.jsonl"
    old3 = tmp_path / "engine-2025-12-31.jsonl"
    for f in (old1, old2, old3):
        f.write_text("{}\n")
    assert all(f.exists() for f in (old1, old2, old3))

    logger = StructuredLogger(tmp_path)
    logger.info(stage="new_day_start")

    today = tmp_path / _todays_log_name()
    assert today.exists(), "today's file should have been created"
    assert not old1.exists(), "old file should have been purged"
    assert not old2.exists()
    assert not old3.exists()


def test_logger_no_purge_on_subsequent_writes_same_day(tmp_path):
    """Once today's file exists, subsequent writes should NOT trigger purge."""
    logger = StructuredLogger(tmp_path)
    logger.info(stage="first_write")
    today = tmp_path / _todays_log_name()
    assert today.exists()

    # Drop an unrelated (older-looking) engine file AFTER today's exists.
    # Because today's file already exists, the next write should NOT delete it.
    fake_old = tmp_path / "engine-2026-05-01.jsonl"
    fake_old.write_text("{}\n")

    logger.info(stage="second_write")

    assert fake_old.exists(), (
        "subsequent writes on the same day must not purge — today's file "
        "already existed when the write began, so is_new_day_file is False"
    )


def test_logger_does_not_touch_non_engine_files(tmp_path):
    """The glob `engine-*.jsonl` is scoped — random files in the log dir stay."""
    keep_1 = tmp_path / "audit.log"
    keep_2 = tmp_path / "engine.jsonl"  # missing date, doesn't match pattern
    keep_3 = tmp_path / "readme.txt"
    for f in (keep_1, keep_2, keep_3):
        f.write_text("keep me\n")

    # Also drop a stale engine-<date>.jsonl that SHOULD be purged
    stale = tmp_path / "engine-2026-01-01.jsonl"
    stale.write_text("{}\n")

    StructuredLogger(tmp_path).info(stage="hello")

    assert not stale.exists(), "stale engine-YYYY-MM-DD.jsonl should be purged"
    assert keep_1.exists()
    assert keep_2.exists()
    assert keep_3.exists()


def test_logger_purge_survives_missing_files(tmp_path):
    """If glob lists a file that vanishes before unlink (race), don't crash."""
    logger = StructuredLogger(tmp_path)
    # No stale files here — call the purge helper directly with a fake current
    # path; it should simply do nothing without erroring.
    logger._purge_stale_log_files(current=tmp_path / "engine-2999-01-01.jsonl")
    # If we got here without exception, we're good.
