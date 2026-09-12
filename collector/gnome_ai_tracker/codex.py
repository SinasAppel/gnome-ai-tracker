"""Codex usage adapter.

Adapted from Omarchy's `omarchy-agent-usage-codex` (MIT, see
THIRD_PARTY_NOTICES). Local stats come from native Codex CLI session files
(`event_msg`/`response_item` carriers of `token_count` payloads), pi/omp
sessions that ran through openai-codex, and opencode sessions that ran on
an OpenAI provider. Rate limits and the plan come from the Codex
app-server RPC (`account/read`, `account/rateLimits/read`).

One deliberate departure from upstream: pi/omp sessions are scanned with
pure Python instead of shelling out to `rg`, so the collector has no
external binary dependency beyond the `codex` CLI itself (and even that is
optional — local history renders without it).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import select
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .common import (
    SCHEMA_VERSION,
    cache_root,
    expand_path,
    local_day,
    locked,
    number,
    read_fresh_json,
    utc_now_iso,
    write_json,
)

AGENT_ID = "codex"
AGENT_NAME = "Codex"
AUTH_HELP = "Run `codex login` to authenticate."

# A scan this recent is only reused to dedup concurrent collector runs
# (periodic refresh plus manual refresh); every periodic refresh otherwise
# lands a real rescan. --limits-only promises only fresh limits, so it may
# reuse a scan for up to 15 minutes.
SCAN_REUSE_SECONDS = 20
LIMITS_ONLY_REUSE_SECONDS = 900

SESSION_WINDOW_DAYS = 30


def codex_home(override: str = "") -> Path:
    if override:
        return expand_path(override)
    return expand_path(os.environ.get("CODEX_HOME") or "~/.codex")


def runtime_env() -> dict[str, str]:
    home = str(Path.home())
    path_parts = [
        os.environ.get("PATH", ""),
        f"{home}/.local/bin",
        f"{home}/.npm-global/bin",
        f"{home}/.local/share/mise/shims",
    ]
    env = os.environ.copy()
    env["PATH"] = os.pathsep.join(part for part in path_parts if part)
    return env


ENV = runtime_env()


def find_command(name: str, override: str = "") -> str:
    if override:
        candidate = expand_path(override)
        if candidate.is_file():
            return str(candidate)
        return override
    return shutil.which(name, path=ENV.get("PATH")) or ""


def model_name(raw: Any) -> str:
    value = str(raw or "codex")
    return value if value else "codex"


class Scanner:
    """Accumulates per-message token usage into the record's stats dict."""

    def __init__(self, today: str, recent_dates: list[str]) -> None:
        self.today = today
        self.recent = {day: {"date": day, "messageCount": 0} for day in recent_dates}
        self.recent_dates = recent_dates
        self.today_tokens_by_model: dict[str, int] = {}
        self.model_usage: dict[str, dict[str, int]] = {}
        self.today_sessions: set[str] = set()
        self.active_days: set[str] = set()
        self.today_prompts = 0
        self.today_total_tokens = 0
        self.total_prompts = 0
        self.total_sessions: set[str] = set()

    def add_usage(
        self,
        day: str,
        session_key: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cache_read: int,
        cache_write: int,
    ) -> None:
        total = input_tokens + output_tokens + cache_read + cache_write
        self.total_prompts += 1
        self.total_sessions.add(session_key)
        self.active_days.add(day)

        bucket = self.model_usage.setdefault(
            model,
            {
                "inputTokens": 0,
                "outputTokens": 0,
                "cacheReadInputTokens": 0,
                "cacheCreationInputTokens": 0,
            },
        )
        bucket["inputTokens"] += input_tokens
        bucket["outputTokens"] += output_tokens
        bucket["cacheReadInputTokens"] += cache_read
        bucket["cacheCreationInputTokens"] += cache_write

        if day in self.recent:
            self.recent[day]["messageCount"] += total

        if day == self.today:
            self.today_prompts += 1
            self.today_sessions.add(session_key)
            self.today_total_tokens += total
            self.today_tokens_by_model[model] = self.today_tokens_by_model.get(model, 0) + total

    def snapshot(self) -> dict[str, Any]:
        return {
            "todayPrompts": self.today_prompts,
            "todaySessions": len(self.today_sessions),
            "todayTotalTokens": self.today_total_tokens,
            "todayTokensByModel": self.today_tokens_by_model,
            "recentDays": [self.recent[day] for day in self.recent_dates],
            "totalPrompts": self.total_prompts,
            "totalSessions": len(self.total_sessions),
            # Days with any recorded usage, for the all-time "N days"
            # summary. The dates travel too: merging snapshots from several
            # machines needs their union, which a count alone cannot give.
            "activeDays": len(self.active_days),
            "activeDates": sorted(self.active_days),
            "modelUsage": self.model_usage,
        }


def scan_pi_sessions(scanner: Scanner) -> None:
    roots = [
        Path.home() / ".pi" / "agent" / "sessions",
        Path.home() / ".omp" / "agent" / "sessions",
    ]
    seen: set[str] = set()
    for root in roots:
        files = root.rglob("*.jsonl") if root.is_dir() else []
        for path in files:
            try:
                with path.open("r", encoding="utf-8", errors="replace") as handle:
                    for line_number, line in enumerate(handle, 1):
                        if '"openai-codex' not in line or '"usage"' not in line:
                            continue
                        try:
                            entry = json.loads(line)
                        except Exception:
                            continue
                        if entry.get("type") != "message":
                            continue
                        message = entry.get("message") or {}
                        if message.get("role") != "assistant":
                            continue
                        provider = str(message.get("provider") or "")
                        api = str(message.get("api") or "")
                        if provider != "openai-codex" and not api.startswith("openai-codex"):
                            continue
                        message_key = f"{path}:{entry.get('id') or line_number}"
                        if message_key in seen:
                            continue
                        seen.add(message_key)
                        usage = message.get("usage") or {}
                        if not usage:
                            continue
                        total = number(usage.get("totalTokens"))
                        input_tokens = number(usage.get("input"))
                        output_tokens = number(usage.get("output"))
                        cache_read = number(usage.get("cacheRead"))
                        cache_write = number(usage.get("cacheWrite"))
                        if total and not (input_tokens or output_tokens or cache_read or cache_write):
                            input_tokens = total
                        if not (input_tokens or output_tokens or cache_read or cache_write):
                            continue
                        day = local_day(entry.get("timestamp") or message.get("timestamp"))
                        scanner.add_usage(
                            day,
                            str(path),
                            model_name(message.get("model")),
                            input_tokens,
                            output_tokens,
                            cache_read,
                            cache_write,
                        )
            except OSError:
                continue


def looks_like_openai_assistant_row(raw: str) -> bool:
    return '"assistant"' in raw and '"openai"' in raw


def scan_opencode_sessions(scanner: Scanner) -> bool:
    """opencode sessions on the `openai` provider (read-only).

    Returns whether the scan ran to completion: a scan cut short by a
    database error still contributes what it read, but must not be cached
    as if it were the whole story.
    """
    db = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")) / "opencode" / "opencode.db"
    if not db.is_file():
        return True
    try:
        conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
        return False
    try:
        conn.execute("PRAGMA query_only = ON")
        # OpenCode DBs grow huge (every historical message, JSON included).
        # The json_extract conditions are the authority for the rows that
        # reach Python: role == "assistant" and providerID == "openai", the
        # same exact-match values the Python filter below checks. The LIKE
        # gates in front are pure acceleration, not a perfect proxy, and
        # json_valid guards the parse itself: json_extract() RAISES on
        # malformed JSON instead of returning NULL, and one such row would
        # otherwise abort the whole scan.
        rows = conn.execute(
            "SELECT session_id, data FROM message"
            " WHERE data LIKE '%\"role\"%:%\"assistant\"%'"
            " AND data LIKE '%\"providerID\"%:%\"openai\"%'"
            " AND CASE WHEN json_valid(data) THEN json_extract(data, '$.role') END = 'assistant'"
            " AND CASE WHEN json_valid(data) THEN json_extract(data, '$.providerID') END = 'openai'"
        )
        for session_id, raw in rows:
            # One malformed row must not abort the scan.
            try:
                entry = json.loads(raw)
                # Exact match: a custom "openai-local" gateway is not this
                # subscription.
                if not isinstance(entry, dict) or entry.get("role") != "assistant":
                    continue
                if str(entry.get("providerID") or "") != "openai":
                    continue
                tokens = entry.get("tokens") or {}
                cache = tokens.get("cache") or {}
                input_tokens = number(tokens.get("input"))
                # opencode keeps thinking tokens out of output; both are generated.
                output_tokens = number(tokens.get("output")) + number(tokens.get("reasoning"))
                cache_read = number(cache.get("read"))
                cache_write = number(cache.get("write"))
                if not (input_tokens or output_tokens or cache_read or cache_write):
                    continue
                day = local_day((entry.get("time") or {}).get("created"))
                model = model_name(str(entry.get("modelID") or "").rstrip("/").split("/")[-1])
            except Exception:
                continue
            scanner.add_usage(day, "opencode:" + str(session_id), model, input_tokens, output_tokens, cache_read, cache_write)
    except sqlite3.Error:
        # Transient lock, schema migration, corruption: the numbers stop
        # here, incomplete.
        return False
    finally:
        conn.close()
    return True


def scan_native_codex_sessions(scanner: Scanner, home: Path) -> None:
    roots = [home / "sessions", home / "archived_sessions"]
    files: list[Path] = []
    cutoff = time.time() - SESSION_WINDOW_DAYS * 24 * 60 * 60
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*.jsonl"):
            try:
                if path.stat().st_mtime >= cutoff:
                    files.append(path)
            except OSError:
                pass

    for path in files:
        current_model = "codex"
        try:
            file_mtime = path.stat().st_mtime
        except OSError:
            continue
        try:
            with path.open(errors="replace") as handle:
                for raw in handle:
                    try:
                        entry = json.loads(raw)
                    except Exception:
                        continue
                    if entry.get("type") == "turn_context":
                        payload = entry.get("payload") or {}
                        current_model = model_name(payload.get("model") or payload.get("model_slug") or current_model)
                        continue
                    payload = entry.get("payload") or entry
                    if entry.get("type") == "response_item" and isinstance(payload, dict):
                        payload = payload.get("payload") or payload
                    if not isinstance(payload, dict):
                        continue
                    if payload.get("type") != "token_count":
                        continue
                    info = payload.get("info") or {}
                    # total_token_usage is cumulative for the session. Adding
                    # every snapshot makes usage grow quadratically, so count
                    # the per-turn delta instead.
                    usage = info.get("last_token_usage") or {}
                    cache_read = number(usage.get("cached_input_tokens"))
                    cache_write = number(usage.get("cache_write_input_tokens"))
                    # Cached tokens are included in input_tokens, and
                    # reasoning tokens are included in output_tokens. Keep
                    # the cache split without counting either twice.
                    input_tokens = max(0, number(usage.get("input_tokens")) - cache_read - cache_write)
                    output_tokens = number(usage.get("output_tokens"))
                    if not (input_tokens or output_tokens or cache_read or cache_write):
                        continue
                    day = local_day(entry.get("timestamp") or file_mtime)
                    scanner.add_usage(day, str(path), current_model, input_tokens, output_tokens, cache_read, cache_write)
        except Exception:
            continue


def scan_cache_paths(home: Path) -> tuple[Path, Path]:
    db = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")) / "opencode" / "opencode.db"
    # The digest covers every data path the scan reads: the codex session
    # roots, the opencode DB, and (via home of the user) the pi/omp roots.
    digest = hashlib.sha1((str(Path.home()) + "\n" + str(home) + "\n" + str(db)).encode("utf-8")).hexdigest()[:16]
    root = cache_root()
    return root / f"codex-scan-{digest}.json", root / f"codex-scan-{digest}.lock"


def read_cached_stats(cache_file: Path, max_age_seconds: float, today: str) -> dict[str, Any] | None:
    """The cache payload is a versioned envelope, so a corrupted or
    foreign-shaped file is a cache miss (rescan + rewrite) instead of a
    crash or a garbage record."""
    cached = read_fresh_json(cache_file, max_age_seconds)
    if not isinstance(cached, dict) or cached.get("schemaVersion") != SCHEMA_VERSION:
        return None
    # today* fields only mean "today" on the day they were scanned. A cache
    # from another local date (midnight passed, or the clock moved) is a
    # miss, not merely old, whatever its mtime says.
    if cached.get("scanDate") != today:
        return None
    stats = cached.get("stats")
    if not isinstance(stats, dict):
        return None
    if not all(key in stats for key in ("todayPrompts", "todayTotalTokens", "recentDays", "activeDates", "modelUsage")):
        return None
    return stats


def write_cached_stats(cache_file: Path, stats: dict[str, Any], today: str) -> None:
    try:
        write_json(cache_file, {"schemaVersion": SCHEMA_VERSION, "scanDate": today, "stats": stats})
    except Exception as exc:
        print(f"codex adapter: could not write usage cache ({exc})", file=sys.stderr)


def run_local_scans(home: Path, today: str, recent_dates: list[str]) -> tuple[dict[str, Any], bool]:
    scanner = Scanner(today, recent_dates)
    scan_pi_sessions(scanner)
    scan_native_codex_sessions(scanner, home)
    complete = scan_opencode_sessions(scanner)
    return scanner.snapshot(), complete


def cached_local_stats(home: Path, max_age: float, today: str, recent_dates: list[str]) -> dict[str, Any]:
    """Local stats, with the cache as a pure optimization.

    The cache must never take the collector down: any cache-layer failure
    degrades to a direct scan and a warning on stderr. The JSON record is
    the contract; the cache is not.
    """
    try:
        return _cached_local_stats(home, max_age, today, recent_dates)
    except Exception as exc:
        print(f"codex adapter: cache unavailable ({exc}); scanning directly", file=sys.stderr)
        stats, _ = run_local_scans(home, today, recent_dates)
        return stats


def _cached_local_stats(home: Path, max_age: float, today: str, recent_dates: list[str]) -> dict[str, Any]:
    cache_file, lock_file = scan_cache_paths(home)

    cached = read_cached_stats(cache_file, max_age, today)
    if cached is not None:
        return cached

    with locked(lock_file):
        cached = read_cached_stats(cache_file, max_age, today)
        if cached is not None:
            return cached
        stats, complete = run_local_scans(home, today, recent_dates)
        # An interrupted scan still serves this run, but caching it would
        # suppress the missing usage for every reader until expiry.
        if complete:
            write_cached_stats(cache_file, stats, today)
        return stats


# ------------------------------------------------------------------- RPC


def rpc_request(proc: subprocess.Popen, request_id: int, method: str, params: dict | None = None, timeout: int = 8):
    payload = {"id": request_id, "method": method, "params": params or {}}
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(payload) + "\n")
    proc.stdin.flush()
    deadline = time.time() + timeout
    while time.time() < deadline:
        assert proc.stdout is not None
        ready, _, _ = select.select([proc.stdout], [], [], 0.25)
        if not ready:
            continue
        line = proc.stdout.readline()
        if not line:
            break
        try:
            message = json.loads(line)
        except Exception:
            continue
        if message.get("id") == request_id:
            return message
    raise TimeoutError(method)


def limit_window(window: Any) -> dict[str, Any] | None:
    if not isinstance(window, dict):
        return None
    used = window.get("usedPercent")
    if used is None:
        return None
    mins = number(window.get("windowDurationMins"))
    if mins == 10080:
        label = "Weekly (7-day)"
    elif mins and mins % 60 == 0:
        label = f"{mins // 60}h window"
    elif mins:
        label = f"{mins}m window"
    else:
        label = "Limit"
    reset = window.get("resetsAt")
    return {
        "label": label,
        "percent": float(used) / 100.0,
        "resetsAt": datetime.fromtimestamp(number(reset), timezone.utc).isoformat() if reset else "",
    }


def fetch_codex_rpc(codex_bin: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {"limits": [], "tierLabel": "", "usageStatusText": "", "authHelpText": AUTH_HELP}
    codex = find_command("codex", codex_bin)
    if not codex:
        result["usageStatusText"] = "Codex unavailable"
        result["authHelpText"] = "codex not found in PATH"
        return result

    try:
        proc = subprocess.Popen(
            [codex, "-s", "read-only", "-a", "on-request", "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            env=ENV,
        )
    except Exception as exc:
        result["usageStatusText"] = "Codex unavailable"
        result["authHelpText"] = str(exc)
        return result

    try:
        rpc_request(proc, 1, "initialize", {"clientInfo": {"name": "gnome-ai-tracker", "version": "1"}}, timeout=8)
        assert proc.stdin is not None
        proc.stdin.write(json.dumps({"method": "initialized", "params": {}}) + "\n")
        proc.stdin.flush()
        account_msg = rpc_request(proc, 2, "account/read", timeout=4)
        limits_msg = rpc_request(proc, 3, "account/rateLimits/read", timeout=4)

        account = (account_msg.get("result") or {}).get("account") or {}
        limits = (limits_msg.get("result") or {}).get("rateLimits") or {}
        plan = limits.get("planType") or account.get("planType") or account.get("type") or ""
        result["tierLabel"] = str(plan) if plan else ""

        for window in (limits.get("primary"), limits.get("secondary")):
            entry = limit_window(window)
            if entry:
                result["limits"].append(entry)
    except Exception as exc:
        result["usageStatusText"] = "Codex limits unavailable"
        result["authHelpText"] = str(exc)
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=1)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    return result


# ------------------------------------------------------------------ record


def collect(
    home: Path | None = None,
    codex_bin: str = "",
    force: bool = False,
    limits_only: bool = False,
) -> dict[str, Any]:
    directory = home or codex_home()
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    recent_dates = [(now - timedelta(days=offset)).strftime("%Y-%m-%d") for offset in range(6, -1, -1)]

    max_age = 0 if force else (LIMITS_ONLY_REUSE_SECONDS if limits_only else SCAN_REUSE_SECONDS)
    stats = cached_local_stats(directory, max_age, today, recent_dates)
    rpc = fetch_codex_rpc(codex_bin)

    record = {
        "schemaVersion": SCHEMA_VERSION,
        "id": AGENT_ID,
        "name": AGENT_NAME,
        "updatedAt": utc_now_iso(),
        "ready": True,
        "hasLocalStats": True,
    }
    record.update(stats)
    record.update(rpc)
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gnome-ai-tracker-codex")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--limits-only", action="store_true")
    parser.add_argument("--codex-home", default="", help="override CODEX_HOME (testing / preferences)")
    parser.add_argument("--codex-bin", default="", help="override codex executable path (testing / preferences)")
    args = parser.parse_args(argv)

    record = collect(
        home=codex_home(args.codex_home) if args.codex_home else None,
        codex_bin=args.codex_bin or os.environ.get("GNOME_AI_TRACKER_CODEX_BIN") or "",
        force=args.force,
        limits_only=args.limits_only,
    )
    print(json.dumps(record, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
