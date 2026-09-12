"""Claude Code usage adapter.

Adapted from Omarchy's `omarchy-agent-usage-claude` (MIT, see
THIRD_PARTY_NOTICES). Everything the panel shows for Claude comes from the
single JSON record this module builds:

  - local transcript stats from `<claude-dir>/projects` (*.jsonl),
  - `stats-cache.json` / `history.jsonl` fallbacks for machines whose
    transcripts are gone,
  - pi/omp and opencode sessions that ran on an Anthropic provider,
  - authoritative rate limits from Anthropic's OAuth usage endpoint,
    using the CLI's own saved login (never refreshed or modified here).

Only the display-safe plan label leaves the credential store; the access
token travels exclusively in the Authorization header of the limits probe.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .common import (
    SCHEMA_VERSION,
    cache_root,
    empty_bucket,
    expand_path,
    local_day,
    locked,
    number,
    read_fresh_json,
    recent_date_strings,
    today_string,
    usage_token,
    utc_now_iso,
    write_json,
)

AGENT_ID = "claude"
AGENT_NAME = "Claude Code"
AUTH_HELP = "Run `claude auth login` to restore authoritative usage."
USAGE_ENDPOINT = "https://api.anthropic.com/api/oauth/usage"
PROBE_MIN_INTERVAL_SECONDS = 15


def config_dir(override: str = "") -> Path:
    if override:
        return expand_path(override)
    return expand_path(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude")


def scan_projects(projects_path: Path) -> dict[str, Any]:
    today = today_string()
    recent_dates = recent_date_strings()
    recent = {day: {"date": day, "messageCount": 0} for day in recent_dates}

    seen: set[str] = set()
    sessions: set[str] = set()
    active_days: set[str] = set()
    today_sessions: set[str] = set()
    today_tokens: dict[str, int] = {}
    usage_by_model: dict[str, dict[str, int]] = {}
    prompts = 0
    today_prompt_count = 0
    today_token_total = 0

    files = projects_path.rglob("*.jsonl") if projects_path.is_dir() else []
    for path in files:
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line_number, line in enumerate(handle, 1):
                    # Cheap pre-filter before JSON parsing keeps files with
                    # unrelated lines inexpensive.
                    if '"usage":' not in line:
                        continue
                    try:
                        entry = json.loads(line)
                    except Exception:
                        continue

                    message = entry.get("message") if isinstance(entry.get("message"), dict) else {}
                    if entry.get("type") != "assistant" and message.get("role") != "assistant":
                        continue

                    usage = message.get("usage") or entry.get("usage")
                    if not isinstance(usage, dict):
                        continue

                    message_id = message.get("id") or entry.get("messageId") or ""
                    if message_id:
                        unique_key = str(message_id)
                    else:
                        unique_key = f"{path}:{entry.get('uuid') or entry.get('requestId') or line_number}"
                    if unique_key in seen:
                        continue
                    seen.add(unique_key)

                    input_tokens = usage_token(usage, "input_tokens", "inputTokens")
                    output_tokens = usage_token(usage, "output_tokens", "outputTokens")
                    cache_read = usage_token(usage, "cache_read_input_tokens", "cacheReadInputTokens")
                    cache_write = usage_token(usage, "cache_creation_input_tokens", "cacheCreationInputTokens")
                    total = input_tokens + output_tokens + cache_read + cache_write
                    if total <= 0:
                        continue

                    model = str(message.get("model") or entry.get("model") or "claude")
                    day = local_day(entry.get("timestamp") or message.get("timestamp"))
                    session_key = str(entry.get("sessionId") or path)
                    sessions.add(session_key)
                    active_days.add(day)
                    prompts += 1

                    bucket = usage_by_model.setdefault(model, empty_bucket())
                    bucket["inputTokens"] += input_tokens
                    bucket["outputTokens"] += output_tokens
                    bucket["cacheReadInputTokens"] += cache_read
                    bucket["cacheCreationInputTokens"] += cache_write

                    if day in recent:
                        # recentDays.messageCount is a token total, despite
                        # the legacy name shared with synced snapshots.
                        recent[day]["messageCount"] += total

                    if day == today:
                        today_prompt_count += 1
                        today_sessions.add(session_key)
                        today_token_total += total
                        today_tokens[model] = today_tokens.get(model, 0) + total
        except Exception as exc:
            print(f"Ignoring unreadable Claude project file {path}: {exc}", file=sys.stderr)

    return {
        "todayPrompts": today_prompt_count,
        "todaySessions": len(today_sessions),
        "todayTotalTokens": today_token_total,
        "todayTokensByModel": today_tokens,
        "recentDays": [recent[day] for day in recent_dates],
        "modelUsage": usage_by_model,
        "totalPrompts": prompts,
        "totalSessions": len(sessions),
        # Days with any recorded usage, for the all-time "N days" summary.
        # The dates travel too: merging snapshots from several machines
        # needs their union, which a count alone cannot give.
        "activeDays": len(active_days),
        "activeDates": sorted(active_days),
    }


def scan_cache_paths(projects_path: Path) -> tuple[Path, Path]:
    digest = hashlib.sha1(str(projects_path).encode("utf-8")).hexdigest()[:16]
    root = cache_root()
    return root / f"claude-scan-{digest}.json", root / f"claude-scan-{digest}.lock"


def cached_scan(projects_path: Path, max_age_seconds: float) -> dict[str, Any]:
    cache_file, lock_file = scan_cache_paths(projects_path)

    cached = read_fresh_json(cache_file, max_age_seconds)
    if isinstance(cached, dict):
        return cached

    with locked(lock_file):
        cached = read_fresh_json(cache_file, max_age_seconds)
        if isinstance(cached, dict):
            return cached
        summary = scan_projects(projects_path)
        write_json(cache_file, summary)
        return summary


def stats_cache_fallback(claude_dir: Path) -> dict[str, Any] | None:
    """Aggregate counters for machines without transcripts on disk."""
    try:
        data = json.loads((claude_dir / "stats-cache.json").read_text(encoding="utf-8"))
    except Exception:
        return None

    today = today_string()
    daily_model_tokens = data.get("dailyModelTokens") or []
    today_tokens: dict[str, int] = {}
    for entry in daily_model_tokens:
        if isinstance(entry, dict) and entry.get("date") == today:
            today_tokens = dict(entry.get("tokensByModel") or {})
            break

    daily_activity = [day for day in (data.get("dailyActivity") or []) if isinstance(day, dict)]
    active_dates = sorted(
        {str(day.get("date")) for day in daily_activity if number(day.get("messageCount")) > 0 and day.get("date")}
    )
    today_prompts, today_sessions = today_prompts_from_history(claude_dir)

    return {
        "todayPrompts": today_prompts,
        "todaySessions": today_sessions,
        "todayTotalTokens": sum(number(v) for v in today_tokens.values()),
        "todayTokensByModel": today_tokens,
        "recentDays": daily_activity[-7:],
        "modelUsage": data.get("modelUsage") or {},
        "totalPrompts": number(data.get("totalMessages")),
        "totalSessions": number(data.get("totalSessions")),
        "activeDays": len(active_dates),
        "activeDates": active_dates,
    }


def today_prompts_from_history(claude_dir: Path) -> tuple[int, int]:
    prompts = 0
    sessions: set[str] = set()
    start_of_day = dt.datetime.combine(dt.datetime.now().date(), dt.time.min).timestamp() * 1000
    try:
        with (claude_dir / "history.jsonl").open("r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
    except Exception:
        return 0, 0

    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except Exception:
            continue
        if number(entry.get("timestamp")) < start_of_day:
            break
        prompts += 1
        if entry.get("sessionId"):
            sessions.add(str(entry.get("sessionId")))
    return prompts, len(sessions)


def _empty_pi_stats() -> dict[str, Any]:
    recent_dates = recent_date_strings()
    return {
        "todayPrompts": 0,
        "todaySessions": 0,
        "todayTotalTokens": 0,
        "todayTokensByModel": {},
        "recentDays": [{"date": day, "messageCount": 0} for day in recent_dates],
        "modelUsage": {},
        "totalPrompts": 0,
        "totalSessions": 0,
        "activeDays": 0,
        "activeDates": [],
    }


def scan_pi_usage(max_age_seconds: float) -> dict[str, Any] | None:
    """pi/omp sessions that consumed a Claude subscription (Anthropic provider)."""
    roots = [
        Path.home() / ".pi" / "agent" / "sessions",
        Path.home() / ".omp" / "agent" / "sessions",
    ]
    cache_file = cache_root() / "claude-pi-sessions.json"
    cached = read_fresh_json(cache_file, max_age_seconds)
    if cached is not None:
        return cached.get("stats")

    today = today_string()
    recent_dates = recent_date_strings()
    recent = {day: {"date": day, "messageCount": 0} for day in recent_dates}
    sessions: set[str] = set()
    active_days: set[str] = set()
    today_sessions: set[str] = set()
    today_tokens: dict[str, int] = {}
    usage_by_model: dict[str, dict[str, int]] = {}
    seen: set[str] = set()
    prompts = 0
    today_prompt_count = 0
    today_token_total = 0

    for root in roots:
        files = root.rglob("*.jsonl") if root.is_dir() else []
        for path in files:
            try:
                with path.open("r", encoding="utf-8", errors="replace") as handle:
                    for line_number, line in enumerate(handle, 1):
                        if '"usage"' not in line or '"assistant"' not in line:
                            continue
                        try:
                            entry = json.loads(line)
                            message = entry.get("message") if isinstance(entry.get("message"), dict) else {}
                            if entry.get("type") != "message" or message.get("role") != "assistant":
                                continue
                            if str(message.get("provider") or "") != "anthropic":
                                continue
                            unique_key = f"{path}:{entry.get('id') or line_number}"
                            if unique_key in seen:
                                continue
                            seen.add(unique_key)
                            usage = message.get("usage") or {}
                            input_tokens = usage_token(usage, "input", "inputTokens")
                            output_tokens = usage_token(usage, "output", "outputTokens")
                            cache_read = usage_token(usage, "cacheRead", "cache_read_input_tokens")
                            cache_write = usage_token(usage, "cacheWrite", "cache_creation_input_tokens")
                            total = input_tokens + output_tokens + cache_read + cache_write
                            if total <= 0:
                                total = number(usage.get("totalTokens"))
                                input_tokens = total
                            if total <= 0:
                                continue
                            model = str(message.get("model") or "claude")
                            day = local_day(entry.get("timestamp") or message.get("timestamp"))
                        except Exception:
                            continue

                        session_key = str(path)
                        sessions.add(session_key)
                        active_days.add(day)
                        prompts += 1
                        bucket = usage_by_model.setdefault(model, empty_bucket())
                        bucket["inputTokens"] += input_tokens
                        bucket["outputTokens"] += output_tokens
                        bucket["cacheReadInputTokens"] += cache_read
                        bucket["cacheCreationInputTokens"] += cache_write
                        if day in recent:
                            recent[day]["messageCount"] += total
                        if day == today:
                            today_prompt_count += 1
                            today_sessions.add(session_key)
                            today_token_total += total
                            today_tokens[model] = today_tokens.get(model, 0) + total
            except OSError:
                continue

    stats = None
    if prompts > 0:
        stats = {
            "todayPrompts": today_prompt_count,
            "todaySessions": len(today_sessions),
            "todayTotalTokens": today_token_total,
            "todayTokensByModel": today_tokens,
            "recentDays": [recent[day] for day in recent_dates],
            "modelUsage": usage_by_model,
            "totalPrompts": prompts,
            "totalSessions": len(sessions),
            "activeDays": len(active_days),
            "activeDates": sorted(active_days),
        }
    write_json(cache_file, {"stats": stats})
    return stats


def opencode_db_path() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")) / "opencode" / "opencode.db"


def scan_opencode_usage(max_age_seconds: float) -> dict[str, Any] | None:
    """opencode sessions that ran on the `anthropic` provider (read-only)."""
    db = opencode_db_path()
    if not db.is_file():
        return None

    cache_file = cache_root() / f"claude-opencode-{hashlib.sha1(str(db).encode('utf-8')).hexdigest()[:16]}.json"
    cached = read_fresh_json(cache_file, max_age_seconds)
    if cached is not None:
        return cached.get("stats")

    today = today_string()
    recent_dates = recent_date_strings()
    recent = {day: {"date": day, "messageCount": 0} for day in recent_dates}
    sessions: set[str] = set()
    active_days: set[str] = set()
    today_sessions: set[str] = set()
    today_tokens: dict[str, int] = {}
    usage_by_model: dict[str, dict[str, int]] = {}
    prompts = 0
    today_prompt_count = 0
    today_token_total = 0

    try:
        conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
        return None
    try:
        conn.execute("PRAGMA query_only = ON")
        rows = conn.execute(
            "SELECT session_id, data FROM message"
            " WHERE data LIKE '%\"role\"%:%\"assistant\"%'"
            " AND data LIKE '%\"providerID\"%:%\"anthropic\"%'"
            " AND CASE WHEN json_valid(data) THEN json_extract(data, '$.role') END = 'assistant'"
            " AND CASE WHEN json_valid(data) THEN json_extract(data, '$.providerID') END = 'anthropic'"
        )
        for session_id, raw in rows:
            # One malformed row must not abort the scan.
            try:
                entry = json.loads(raw)
                # Exact match: a custom "anthropic-proxy" gateway is not
                # this subscription.
                if not isinstance(entry, dict) or entry.get("role") != "assistant":
                    continue
                if str(entry.get("providerID") or "") != "anthropic":
                    continue
                tokens = entry.get("tokens") or {}
                cache = tokens.get("cache") or {}
                input_tokens = number(tokens.get("input"))
                # opencode keeps thinking tokens out of output; both are generated.
                output_tokens = number(tokens.get("output")) + number(tokens.get("reasoning"))
                cache_read = number(cache.get("read"))
                cache_write = number(cache.get("write"))
                total = input_tokens + output_tokens + cache_read + cache_write
                if total <= 0:
                    continue
                created = number((entry.get("time") or {}).get("created"))
                day = local_day(created if created > 0 else None)
                model = str(entry.get("modelID") or "claude").rstrip("/").split("/")[-1]
            except Exception:
                continue
            session_key = "opencode:" + str(session_id)
            sessions.add(session_key)
            active_days.add(day)
            prompts += 1

            bucket = usage_by_model.setdefault(model, empty_bucket())
            bucket["inputTokens"] += input_tokens
            bucket["outputTokens"] += output_tokens
            bucket["cacheReadInputTokens"] += cache_read
            bucket["cacheCreationInputTokens"] += cache_write

            if day in recent:
                recent[day]["messageCount"] += total
            if day == today:
                today_prompt_count += 1
                today_sessions.add(session_key)
                today_token_total += total
                today_tokens[model] = today_tokens.get(model, 0) + total
    except sqlite3.Error:
        return None
    finally:
        conn.close()

    stats = None
    if prompts > 0:
        stats = {
            "todayPrompts": today_prompt_count,
            "todaySessions": len(today_sessions),
            "todayTotalTokens": today_token_total,
            "todayTokensByModel": today_tokens,
            "recentDays": [recent[day] for day in recent_dates],
            "modelUsage": usage_by_model,
            "totalPrompts": prompts,
            "totalSessions": len(sessions),
            "activeDays": len(active_days),
            "activeDates": sorted(active_days),
        }
    write_json(cache_file, {"stats": stats})
    return stats


def merge_stats(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key in ("todayPrompts", "todaySessions", "todayTotalTokens", "totalPrompts", "totalSessions"):
        merged[key] = number(base.get(key)) + number(extra.get(key))

    combined = dict(base.get("todayTokensByModel") or {})
    for model, count in (extra.get("todayTokensByModel") or {}).items():
        combined[model] = number(combined.get(model)) + number(count)
    merged["todayTokensByModel"] = combined

    usage = {model: dict(bucket) for model, bucket in (base.get("modelUsage") or {}).items()}
    for model, bucket in (extra.get("modelUsage") or {}).items():
        target = usage.setdefault(model, empty_bucket())
        for field, count in (bucket or {}).items():
            target[field] = number(target.get(field)) + number(count)
    merged["modelUsage"] = usage

    by_date: dict[str, int] = {}
    for source in (base.get("recentDays") or [], extra.get("recentDays") or []):
        for day in source:
            date = str((day or {}).get("date") or "")
            if date:
                by_date[date] = by_date.get(date, 0) + number((day or {}).get("messageCount"))
    merged["recentDays"] = [{"date": date, "messageCount": by_date[date]} for date in sorted(by_date)]

    # Sources overlap in time, so union dates rather than summing counts.
    dates = set(base.get("activeDates") or []) | set(extra.get("activeDates") or [])
    merged["activeDates"] = sorted(dates)
    merged["activeDays"] = max(len(dates), number(base.get("activeDays")), number(extra.get("activeDays")))
    return merged


# ------------------------------------------------------------------ limits
#
# The access token, its expiry, and the display-safe plan label come from
# the CLI's login. Nothing else leaves the credential store.


def oauth_login(claude_dir: Path) -> tuple[str, int, str]:
    try:
        data = json.loads((claude_dir / ".credentials.json").read_text(encoding="utf-8"))
    except Exception:
        return "", 0, ""
    login = data.get("claudeAiOauth")
    if not isinstance(login, dict):
        return "", 0, ""
    plan = plan_label(str(login.get("rateLimitTier") or ""), str(login.get("subscriptionType") or ""))
    return str(login.get("accessToken") or ""), number(login.get("expiresAt")), plan


def plan_label(tier: str, subscription: str) -> str:
    if tier:
        match = re.search(r"max_(\d+x)", tier, re.IGNORECASE)
        if match:
            return "Max " + match.group(1)
    if subscription:
        return subscription[0].upper() + subscription[1:]
    return ""


def parse_utilization(value: Any) -> float:
    try:
        return float(str(value).strip().replace("%", ""))
    except Exception:
        return float("nan")


def normalize_utilization(value: Any, percent_scale: bool) -> float:
    n = parse_utilization(value)
    if not (n >= 0):
        return -1.0
    # The OAuth usage endpoint currently reports percentages (37.0, 1.0).
    # Older payloads sometimes used fractions (0.37). A payload containing
    # any value >= 1 is percent-scaled, so 1.0 renders as 1%, not 100%.
    if percent_scale or n > 1:
        return min(1.0, n / 100.0)
    return min(1.0, n)


def normalize_reset_at(value: Any) -> str:
    if value is None:
        return ""
    raw = str(value).strip()
    if raw == "":
        return ""
    if raw.isdigit():
        ts = int(raw)
        if ts < 1e12:
            ts *= 1000
        try:
            return dt.datetime.fromtimestamp(ts / 1000, dt.timezone.utc).isoformat()
        except Exception:
            return raw
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed.isoformat()
    except Exception:
        return raw


def usage_bucket(payload: dict[str, Any], key: str) -> dict[str, Any] | None:
    bucket = payload.get(key)
    return bucket if isinstance(bucket, dict) else None


def scoped_window(kind: str) -> str:
    text = kind.lower()
    if "month" in text:
        return "Monthly"
    if "week" in text or "day" in text:
        return "Weekly"
    if "hour" in text or "session" in text:
        return "Session"
    return ""


def scoped_limits(payload: dict[str, Any], percent_scale: bool) -> list[dict[str, Any]]:
    """Model-scoped allowances, which only appear in the `limits` array."""
    entries = payload.get("limits")
    if not isinstance(entries, list):
        return []
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        scope = entry.get("scope")
        model = scope.get("model") if isinstance(scope, dict) else None
        if not isinstance(model, dict):
            continue
        name = str(model.get("display_name") or model.get("id") or "").strip()
        kind = str(entry.get("kind") or "").strip()
        if name == "" or (name, kind) in seen:
            continue
        percent = normalize_utilization(entry.get("percent"), percent_scale)
        if percent < 0:
            continue
        seen.add((name, kind))
        window = scoped_window(kind)
        title = name + " " + window if window else name
        out.append(
            {
                "label": title,
                "title": title,
                "percent": percent,
                "resetsAt": normalize_reset_at(entry.get("resets_at")),
            }
        )
    return out


def probe_limits(access_token: str) -> dict[str, Any]:
    request = urllib.request.Request(
        USAGE_ENDPOINT,
        headers={
            "Authorization": "Bearer " + access_token,
            "anthropic-beta": "oauth-2025-04-20",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as error:
        retry_after = error.headers.get("retry-after", "") if error.headers else ""
        if error.code == 429:
            help_text = "Anthropic's usage endpoint is rate limiting checks right now" + (
                f" (retry after {retry_after}s)" if retry_after else ""
            ) + ". Local Claude Code stats are still shown."
        else:
            help_text = f"Anthropic's usage endpoint returned status {error.code}. Local Claude Code stats are still shown."
        return {"ok": False, "helpText": help_text}
    except Exception:
        # A transport failure reached no server at all. Any real answer,
        # including an error status, is a server we should stop pestering;
        # this is not.
        return {
            "ok": False,
            "transport": True,
            "helpText": "Couldn't reach Anthropic's usage endpoint. Retrying shortly. Local Claude Code stats are still shown.",
        }

    weekly = usage_bucket(payload, "seven_day_oauth_apps") or usage_bucket(payload, "seven_day")
    session = usage_bucket(payload, "five_hour")
    raw = [session.get("utilization") if session else None, weekly.get("utilization") if weekly else None]
    entries = payload.get("limits")
    if isinstance(entries, list):
        raw += [entry.get("percent") for entry in entries if isinstance(entry, dict)]
    percent_scale = any(parse_utilization(v) >= 1 for v in raw)

    limits = []
    if session is not None:
        percent = normalize_utilization(session.get("utilization"), percent_scale)
        if percent >= 0:
            limits.append(
                {
                    "label": "Session (5-hour)",
                    "percent": percent,
                    "resetsAt": normalize_reset_at(session.get("resets_at")),
                }
            )
    if weekly is not None:
        percent = normalize_utilization(weekly.get("utilization"), percent_scale)
        if percent >= 0:
            limits.append(
                {
                    "label": "Weekly (7-day)",
                    "percent": percent,
                    "resetsAt": normalize_reset_at(weekly.get("resets_at")),
                }
            )
    limits.extend(scoped_limits(payload, percent_scale))

    if not limits:
        return {
            "ok": False,
            "helpText": "Anthropic's usage endpoint returned no limits. Local Claude Code stats are still shown.",
        }
    return {"ok": True, "limits": limits}


def limit_window_open(entry: dict[str, Any], now: dt.datetime) -> bool:
    """A cached percentage survives only until its window resets."""
    raw = str(entry.get("resetsAt") or "")
    if raw == "":
        return True
    try:
        resets_at = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return True
    if resets_at.tzinfo is None:
        resets_at = resets_at.replace(tzinfo=dt.timezone.utc)
    return resets_at > now


def usable_cached_limits(cached: dict[str, Any]) -> list[dict[str, Any]]:
    entries = cached.get("limits")
    if not isinstance(entries, list):
        return []
    now = dt.datetime.now(dt.timezone.utc)
    return [entry for entry in entries if isinstance(entry, dict) and limit_window_open(entry, now)]


def collect_limits(access_token: str, expires_at_ms: int, force: bool) -> dict[str, Any]:
    result: dict[str, Any] = {"limits": [], "usageStatusText": "", "authHelpText": AUTH_HELP}

    # A panel opened and shut repeatedly must not turn into a request per
    # flick, so recent probe results are reused for a short window — and
    # kept as the answer of record when a later probe fails.
    probe_cache = cache_root() / "claude-limits.json"
    cached = read_fresh_json(probe_cache, float("inf")) or {}
    if not isinstance(cached, dict):
        cached = {}
    fallback = usable_cached_limits(cached)

    # Probing needs a live token and only the Claude Code CLI can mint one:
    # it refreshes the credential file when it runs, so a machine left alone
    # long enough finds the saved token lapsed. Say so, and keep showing the
    # last numbers whose window has not since reset.
    if access_token == "":
        result["limits"] = fallback
        result["usageStatusText"] = "Waiting for auth"
        return result
    if expires_at_ms > 0 and expires_at_ms <= time.time() * 1000:
        result["limits"] = fallback
        result["usageStatusText"] = "Sign-in expired"
        result["authHelpText"] = (
            "Claude Code's saved sign-in expired"
            + (" — showing the last known limits." if fallback else ".")
            + " Start Claude Code, or run `claude auth login`, to refresh it."
        )
        return result

    # --force is a person asking for fresh numbers: skip the reuse window.
    fetched_at = number(cached.get("fetchedAtMs")) / 1000
    if fallback and not force and time.time() - fetched_at < PROBE_MIN_INTERVAL_SECONDS:
        result["limits"] = fallback
        return result

    probe = probe_limits(access_token)
    if probe["ok"]:
        result["limits"] = probe["limits"]
        write_json(probe_cache, {"fetchedAtMs": round(time.time() * 1000), "limits": probe["limits"]})
        return result

    if probe.get("transport"):
        result["retryAdvised"] = True
    if fallback:
        result["limits"] = fallback
    else:
        result["usageStatusText"] = "Claude limits unavailable"
        result["authHelpText"] = probe["helpText"]
    return result


# ------------------------------------------------------------------ record


def collect(
    claude_dir: Path | None = None,
    force: bool = False,
    limits_only: bool = False,
    cache_seconds: float = 20,
) -> dict[str, Any]:
    directory = claude_dir or config_dir()
    scan_age = 0 if force else (900 if limits_only else cache_seconds)
    stats = cached_scan(directory / "projects", scan_age)

    if number(stats.get("totalPrompts")) <= 0:
        fallback = stats_cache_fallback(directory)
        if fallback is not None:
            stats = fallback
        else:
            # No transcripts and no aggregate cache, but history.jsonl
            # alone can still put numbers on today.
            today_prompts, today_sessions = today_prompts_from_history(directory)
            if today_prompts or today_sessions:
                stats = dict(stats, todayPrompts=today_prompts, todaySessions=today_sessions)

    pi_usage = scan_pi_usage(scan_age)
    if pi_usage is not None:
        stats = merge_stats(stats, pi_usage)

    opencode = scan_opencode_usage(scan_age)
    if opencode is not None:
        stats = merge_stats(stats, opencode)

    access_token, expires_at_ms, plan = oauth_login(directory)
    limits = collect_limits(access_token, expires_at_ms, force)

    record = {
        "schemaVersion": SCHEMA_VERSION,
        "id": AGENT_ID,
        "name": AGENT_NAME,
        "updatedAt": utc_now_iso(),
        "ready": number(stats.get("totalPrompts")) > 0 or len(limits["limits"]) > 0,
        "hasLocalStats": True,
        "tierLabel": plan,
        "usageStatusText": limits["usageStatusText"],
        "authHelpText": limits["authHelpText"],
        "limits": limits["limits"],
    }
    if limits.get("retryAdvised"):
        record["retryAdvised"] = True
    record.update(stats)
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gnome-ai-tracker-claude")
    parser.add_argument("--force", action="store_true", help="rescan transcripts and re-probe limits, ignoring caches")
    parser.add_argument(
        "--limits-only", action="store_true", help="reuse any recent transcript scan; only the limits probe must be fresh"
    )
    parser.add_argument("--cache-seconds", type=float, default=20)
    parser.add_argument("--claude-dir", default="", help="override CLAUDE_CONFIG_DIR (testing / preferences)")
    args = parser.parse_args(argv)

    record = collect(
        claude_dir=config_dir(args.claude_dir) if args.claude_dir else None,
        force=args.force,
        limits_only=args.limits_only,
        cache_seconds=args.cache_seconds,
    )
    print(json.dumps(record, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
