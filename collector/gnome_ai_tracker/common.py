"""Shared helpers for the GNOME AI Tracker collector.

The collector is a bundled Python 3 program with one provider adapter per
AI coding subscription. Each adapter prints a single normalized,
display-safe JSON snapshot to stdout. The GNOME Shell UI only ever reads
that JSON; it never touches transcript formats, databases, or endpoints.

Contract rules enforced here and in the adapters:
  - Snapshots carry schema version, provider id, display name, optional
    plan label, limit buckets with used fraction + reset timestamp, and
    daily/model token totals with explicit date coverage.
  - Credentials never appear in snapshots, logs, or error text. The
    Claude OAuth token is only ever sent in the Authorization header of
    the limits probe.
  - Account limits and machine-local history are different measurements.
    Missing data means unknown, never zero.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

# Cache lives under XDG_CACHE_HOME/gnome-ai-tracker per the project plan.
# Only aggregates and minimal indexing metadata are stored here, never
# prompt content or credentials.
CACHE_DIR_NAME = "gnome-ai-tracker"


def cache_root() -> Path:
    root = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache"))
    root = root / CACHE_DIR_NAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def expand_path(value: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(value))).resolve()


def date_string(value: dt.date) -> str:
    return value.strftime("%Y-%m-%d")


def today_string() -> str:
    return date_string(dt.datetime.now().date())


def recent_date_strings(days: int = 7) -> list[str]:
    today = dt.datetime.now().date()
    return [date_string(today - dt.timedelta(days=offset)) for offset in range(days - 1, -1, -1)]


def local_day(value: Any) -> str:
    """Bucket an arbitrary timestamp into a local calendar date string."""
    if value is None:
        return today_string()
    if isinstance(value, (int, float)):
        try:
            seconds = float(value) / 1000.0 if float(value) > 10_000_000_000 else float(value)
            return date_string(dt.datetime.fromtimestamp(seconds).date())
        except Exception:
            return today_string()
    raw = str(value).strip()
    if not raw:
        return today_string()
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone()
        return date_string(parsed.date())
    except Exception:
        return today_string()


def number(value: Any) -> int:
    try:
        n = float(value or 0)
        return round(n) if n == n else 0
    except Exception:
        return 0


def usage_token(usage: dict[str, Any], snake_key: str, camel_key: str) -> int:
    value = usage.get(snake_key, usage.get(camel_key, 0))
    try:
        return round(float(value or 0))
    except Exception:
        return 0


def empty_bucket() -> dict[str, int]:
    return {
        "inputTokens": 0,
        "outputTokens": 0,
        "cacheReadInputTokens": 0,
        "cacheCreationInputTokens": 0,
    }


def read_fresh_json(path: Path, max_age_seconds: float) -> Any | None:
    if max_age_seconds <= 0 or not path.exists():
        return None
    try:
        # A negative age means the mtime is in the future (the clock moved
        # backwards since the write), so freshness cannot be trusted.
        age = time.time() - path.stat().st_mtime
        if 0 <= age <= max_age_seconds:
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return None


def write_json(path: Path, payload: Any) -> None:
    # A temp name unique to this writer: several collectors can run at once
    # (periodic refresh plus manual refresh), and a shared temp path means
    # the second replace finds the first one's file already moved away.
    handle_fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n")
        # mkstemp opens at 0600; nothing cached here is sensitive.
        tmp.chmod(0o644)
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def locked(path: Path):
    """Yield an exclusive lock file handle for cache-fill critical sections."""

    class _Lock:
        def __enter__(self):
            self.handle = path.open("w")
            fcntl.flock(self.handle, fcntl.LOCK_EX)
            return self.handle

        def __exit__(self, *exc):
            try:
                fcntl.flock(self.handle, fcntl.LOCK_UN)
            finally:
                self.handle.close()
            return False

    return _Lock()


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()
