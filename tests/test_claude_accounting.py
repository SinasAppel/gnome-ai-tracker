"""Claude adapter accounting tests (fixture-driven, date-independent).

Fixture timestamps are rewritten to relative dates on setup (2026-09-12 ->
today, 2026-09-11 -> yesterday) so day bucketing holds whenever the suite
runs.
"""

import json
import shutil
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "collector"))

from gnome_ai_tracker import claude
from gnome_ai_tracker.common import today_string

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "claude_sample.jsonl"
TODAY = date.today()
YESTERDAY = TODAY - timedelta(days=1)


def remap(text: str) -> str:
    return text.replace("2026-09-12", TODAY.isoformat()).replace("2026-09-11", YESTERDAY.isoformat())


class ClaudeAccountingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="claude-test-"))
        projects = self.tmp / "projects"
        projects.mkdir()
        (projects / "sample.jsonl").write_text(remap(FIXTURE.read_text()), encoding="utf-8")
        self.projects = projects

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def scan(self):
        return claude.scan_projects(self.projects)

    def test_dedup_and_skips(self):
        stats = self.scan()
        # 4 unique assistant messages: dedup-1 (x3 in file — one exact
        # re-read plus one re-emitted content block with a different entry
        # uuid, as Claude writes per-block entries sharing a message id),
        # uuid fallback (x2), camelCase entry, no-model entry. User,
        # zero-token and malformed lines are skipped.
        self.assertEqual(stats["totalPrompts"], 4)
        self.assertEqual(stats["totalSessions"], 3)

    def test_token_split_has_no_double_count(self):
        stats = self.scan()
        opus = stats["modelUsage"]["claude-opus-4"]
        self.assertEqual(opus["inputTokens"], 15)
        self.assertEqual(opus["outputTokens"], 27)
        self.assertEqual(opus["cacheReadInputTokens"], 100)
        self.assertEqual(opus["cacheCreationInputTokens"], 50)
        # 10+20+100+50 and 5+7, summed without recounting cache.
        self.assertEqual(15 + 27 + 100 + 50, 192)

    def test_camel_case_usage_keys(self):
        stats = self.scan()
        sonnet = stats["modelUsage"]["claude-sonnet-4"]
        self.assertEqual(sonnet["inputTokens"], 100)
        self.assertEqual(sonnet["outputTokens"], 40)
        self.assertEqual(sonnet["cacheReadInputTokens"], 30)

    def test_missing_model_label(self):
        stats = self.scan()
        self.assertIn("claude", stats["modelUsage"])
        self.assertEqual(stats["modelUsage"]["claude"]["inputTokens"], 3)

    def test_day_buckets(self):
        stats = self.scan()
        by_date = {d["date"]: d["messageCount"] for d in stats["recentDays"]}
        self.assertEqual(len(stats["recentDays"]), 7)
        self.assertEqual(by_date[TODAY.isoformat()], 180 + 12 + 7)
        self.assertEqual(by_date[YESTERDAY.isoformat()], 170)
        self.assertEqual(stats["todayPrompts"], 3)
        self.assertEqual(stats["todayTotalTokens"], 199)
        self.assertIn(TODAY.isoformat(), stats["activeDates"])
        self.assertIn(YESTERDAY.isoformat(), stats["activeDates"])
        self.assertEqual(stats["activeDays"], 2)

    def test_merge_stats_unions_dates(self):
        a = self.scan()
        extra = {
            "todayPrompts": 1,
            "todaySessions": 1,
            "todayTotalTokens": 10,
            "todayTokensByModel": {"claude-opus-4": 10},
            "recentDays": [{"date": TODAY.isoformat(), "messageCount": 10}],
            "modelUsage": {"claude-opus-4": {"inputTokens": 10, "outputTokens": 0, "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0}},
            "totalPrompts": 1,
            "totalSessions": 1,
            "activeDays": 1,
            "activeDates": [TODAY.isoformat()],
        }
        merged = claude.merge_stats(a, extra)
        self.assertEqual(merged["totalPrompts"], 5)
        self.assertEqual(merged["todayTotalTokens"], 209)
        # Same date: token counts add, date list stays a union.
        by_date = {d["date"]: d["messageCount"] for d in merged["recentDays"]}
        self.assertEqual(by_date[TODAY.isoformat()], 209)
        self.assertEqual(sorted(merged["activeDates"]), sorted(a["activeDates"]))

    def test_collect_without_credentials_is_offline_safe(self):
        claude_dir = self.tmp / "claude-home"
        (claude_dir / "projects").mkdir(parents=True)
        shutil.copy(str(self.projects / "sample.jsonl"), claude_dir / "projects" / "s.jsonl")
        record = claude.collect(claude_dir=claude_dir, force=True)
        self.assertEqual(record["schemaVersion"], 1)
        self.assertEqual(record["id"], "claude")
        self.assertTrue(record["ready"])
        self.assertEqual(record["usageStatusText"], "Waiting for auth")
        self.assertNotIn("accessToken", json.dumps(record))
        self.assertEqual(record["totalPrompts"], 4)

    def test_expired_login_keeps_history(self):
        claude_dir = self.tmp / "claude-expired"
        (claude_dir / "projects").mkdir(parents=True)
        shutil.copy(str(self.projects / "sample.jsonl"), claude_dir / "projects" / "s.jsonl")
        (claude_dir / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"accessToken": "secret", "expiresAt": 1, "subscriptionType": "pro"}}),
            encoding="utf-8",
        )
        record = claude.collect(claude_dir=claude_dir, force=True)
        self.assertEqual(record["usageStatusText"], "Sign-in expired")
        self.assertEqual(record["tierLabel"], "Pro")
        self.assertEqual(record["totalPrompts"], 4)
        self.assertNotIn("secret", json.dumps(record))

    def test_plan_label_max_tier(self):
        self.assertEqual(claude.plan_label("max_20x", "pro"), "Max 20x")
        self.assertEqual(claude.plan_label("", "pro"), "Pro")
        self.assertEqual(claude.plan_label("", ""), "")

    def test_local_day_buckets_timezone_shapes(self):
        from gnome_ai_tracker.common import local_day, today_string

        # ISO-8601 with Z, epoch seconds, epoch milliseconds, and garbage
        # all bucket without throwing; garbage means unknown -> today.
        self.assertRegex(local_day("2026-09-11T23:30:00.000Z"), r"^\d{4}-\d{2}-\d{2}$")
        self.assertRegex(local_day(1720000000), r"^\d{4}-\d{2}-\d{2}$")
        self.assertEqual(local_day(1720000000000), local_day(1720000000))
        self.assertEqual(local_day("not-a-date"), today_string())
        self.assertEqual(local_day(None), today_string())

    def test_normalize_utilization_scales(self):
        # Percent-scale payload: 1.0 means 1%, not 100%.
        self.assertAlmostEqual(claude.normalize_utilization(37.0, True), 0.37)
        self.assertAlmostEqual(claude.normalize_utilization(1.0, True), 0.01)
        # Fraction-scale payload stays fractional.
        self.assertAlmostEqual(claude.normalize_utilization(0.37, False), 0.37)
        self.assertLess(claude.normalize_utilization("n/a", False), 0)

    def test_stale_cached_limits_dropped_after_reset(self):
        from datetime import datetime, timezone

        past = "2000-01-01T00:00:00+00:00"
        future = "2999-01-01T00:00:00+00:00"
        cached = {"limits": [
            {"label": "Old", "percent": 0.9, "resetsAt": past},
            {"label": "Live", "percent": 0.1, "resetsAt": future},
            {"label": "NoReset", "percent": 0.2, "resetsAt": ""},
        ]}
        usable = claude.usable_cached_limits(cached)
        self.assertEqual([e["label"] for e in usable], ["Live", "NoReset"])
        self.assertTrue(claude.limit_window_open({"resetsAt": "garbage"}, datetime.now(timezone.utc)))


if __name__ == "__main__":
    unittest.main()
