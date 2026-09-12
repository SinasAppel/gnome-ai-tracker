"""Codex adapter accounting tests (fixture-driven, date-independent).

Fixture timestamps are rewritten to relative dates on setup (2026-09-12 ->
today, 2026-09-11 -> yesterday) so day bucketing holds whenever the suite
runs. Local scans only — the app-server RPC is validated live, not here.
"""

import shutil
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "collector"))

from gnome_ai_tracker import codex

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "codex_sample.jsonl"
TODAY = date.today()
YESTERDAY = TODAY - timedelta(days=1)


def remap(text: str) -> str:
    return text.replace("2026-09-12", TODAY.isoformat()).replace("2026-09-11", YESTERDAY.isoformat())


class CodexAccountingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="codex-test-"))
        home = self.tmp / "codex-home"
        sessions = home / "sessions"
        sessions.mkdir(parents=True)
        (sessions / "sample.jsonl").write_text(remap(FIXTURE.read_text()), encoding="utf-8")
        self.home = home
        self.today = TODAY.isoformat()
        self.recent = [(TODAY - timedelta(days=o)).isoformat() for o in range(6, -1, -1)]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def scan(self):
        stats, complete = codex.run_local_scans(self.home, self.today, self.recent)
        self.assertTrue(complete)
        return stats

    def test_cumulative_counters_counted_as_deltas(self):
        stats = self.scan()
        gpt5 = stats["modelUsage"]["gpt-5"]
        # Turn deltas summed: (1000-200-100, 300, 200, 100) + (1500-500, 100, 500, 0).
        self.assertEqual(gpt5["inputTokens"], 700 + 1000)
        self.assertEqual(gpt5["outputTokens"], 400)
        self.assertEqual(gpt5["cacheReadInputTokens"], 700)
        self.assertEqual(gpt5["cacheCreationInputTokens"], 100)
        # The cumulative total_token_usage (2900) must NOT be added on top.
        self.assertEqual(stats["todayTotalTokens"], 1300 + 1600 + 90)

    def test_cached_tokens_not_double_counted(self):
        stats = self.scan()
        gpt5 = stats["modelUsage"]["gpt-5"]
        # input bucket excludes cache; adding every bucket reproduces the
        # wire total exactly (2900 across the two gpt-5 turns).
        self.assertEqual(
            gpt5["inputTokens"] + gpt5["outputTokens"] + gpt5["cacheReadInputTokens"] + gpt5["cacheCreationInputTokens"],
            2900,
        )

    def test_model_change_and_day_rollover(self):
        stats = self.scan()
        other = stats["modelUsage"]["gpt-5-codex"]
        # Today's turn after the model switch plus yesterday's wrapped event.
        self.assertEqual(other["inputTokens"], 30 + 40)
        self.assertEqual(other["outputTokens"], 30 + 10)
        by_date = {d["date"]: d["messageCount"] for d in stats["recentDays"]}
        self.assertEqual(by_date[self.today], 2990)
        self.assertEqual(by_date[YESTERDAY.isoformat()], 50)

    def test_counts_and_skips(self):
        stats = self.scan()
        # 4 token_count events with nonzero usage; the user message,
        # malformed line and zero-total event are skipped.
        self.assertEqual(stats["totalPrompts"], 4)
        self.assertEqual(stats["totalSessions"], 1)
        self.assertEqual(stats["todayPrompts"], 3)
        self.assertEqual(stats["todaySessions"], 1)
        self.assertEqual(stats["activeDays"], 2)

    def test_old_sessions_outside_window_ignored(self):
        old = self.home / "sessions" / "old.jsonl"
        old.write_text(
            '{"timestamp": "2020-01-01T00:00:00Z", "type": "event_msg",'
            ' "payload": {"type": "token_count", "info": {"last_token_usage":'
            ' {"input_tokens": 99999, "output_tokens": 99999, "total_tokens": 199998}}}}}\n',
            encoding="utf-8",
        )
        import os

        ancient = datetime(2020, 1, 2).timestamp()
        os.utime(old, (ancient, ancient))
        stats = self.scan()
        self.assertEqual(stats["totalPrompts"], 4)

    def test_limit_window_labels(self):
        self.assertEqual(codex.limit_window({"usedPercent": 9.0, "windowDurationMins": 300, "resetsAt": 1})["label"], "5h window")
        self.assertEqual(
            codex.limit_window({"usedPercent": 15.0, "windowDurationMins": 10080, "resetsAt": 1})["label"], "Weekly (7-day)"
        )
        self.assertIsNone(codex.limit_window({"windowDurationMins": 300}))
        self.assertIsNone(codex.limit_window(None))

    def test_missing_binary_reports_unavailable(self):
        rpc = codex.fetch_codex_rpc(codex_bin="/nonexistent/codex-binary")
        self.assertEqual(rpc["usageStatusText"], "Codex unavailable")
        self.assertEqual(rpc["limits"], [])


if __name__ == "__main__":
    unittest.main()
