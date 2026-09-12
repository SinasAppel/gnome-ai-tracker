"""Snapshot contract tests: the guarantees the GNOME UI relies on.

Every provider record — live or fixture — must satisfy these, because the
Shell popup renders whatever the collector prints. Mirrors the validation
rules in `lib/snapshot.js`; both must stay in sync.
"""

import json
import shutil
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "collector"))

from gnome_ai_tracker import claude, codex
from gnome_ai_tracker.common import SCHEMA_VERSION

FORBIDDEN_SUBSTRINGS = ("accessToken", "refreshToken", "apiKey", "api_key", "OPENAI_API_KEY")


def check_contract(test: unittest.TestCase, record: dict, provider_id: str) -> None:
    test.assertEqual(record["schemaVersion"], SCHEMA_VERSION)
    test.assertEqual(record["id"], provider_id)
    test.assertIsInstance(record["name"], str)
    test.assertTrue(record["name"])
    test.assertIsInstance(record["updatedAt"], str)
    test.assertIsInstance(record["ready"], bool)
    test.assertIsInstance(record["hasLocalStats"], bool)

    for key in ("todayPrompts", "todaySessions", "todayTotalTokens", "totalPrompts", "totalSessions", "activeDays"):
        test.assertIsInstance(record[key], int, key)
        test.assertGreaterEqual(record[key], 0, key)

    recent = record["recentDays"]
    test.assertEqual(len(recent), 7, "seven calendar days including today")
    dates = [d["date"] for d in recent]
    test.assertIn(date.today().isoformat(), dates, "coverage includes today")
    test.assertEqual(dates, sorted(dates))
    for day in recent:
        test.assertIsInstance(day["messageCount"], int)
        test.assertGreaterEqual(day["messageCount"], 0)

    test.assertEqual(record["activeDays"], len(record["activeDates"]))
    test.assertEqual(record["activeDates"], sorted(record["activeDates"]))

    for model, bucket in record["modelUsage"].items():
        test.assertIsInstance(model, str)
        for field in ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens"):
            test.assertIsInstance(bucket[field], int, f"{model}.{field}")
            test.assertGreaterEqual(bucket[field], 0, f"{model}.{field}")

    test.assertIsInstance(record["limits"], list)
    for limit in record["limits"]:
        test.assertIsInstance(limit["label"], str)
        pct = limit["percent"]
        test.assertIsInstance(pct, float)
        test.assertGreaterEqual(pct, 0.0)
        test.assertLessEqual(pct, 1.0, "used fraction, never provider percent")
        test.assertIsInstance(limit["resetsAt"], str)

    blob = json.dumps(record)
    for secret in FORBIDDEN_SUBSTRINGS:
        test.assertNotIn(secret, blob, "no credential material in snapshots")


class SnapshotContractTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="contract-test-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_claude_offline_record(self):
        home = self.tmp / "claude"
        (home / "projects").mkdir(parents=True)
        (home / "projects" / "s.jsonl").write_text(
            '{"type":"assistant","timestamp":"%sT10:00:00Z","sessionId":"s1","uuid":"u1",'
            '"message":{"id":"m1","model":"claude-opus-4","role":"assistant",'
            '"usage":{"input_tokens":10,"output_tokens":5}}}}\n' % date.today().isoformat(),
            encoding="utf-8",
        )
        check_contract(self, claude.collect(claude_dir=home, force=True), "claude")

    def test_codex_record_without_binary(self):
        home = self.tmp / "codex"
        (home / "sessions").mkdir(parents=True)
        check_contract(self, codex.collect(home=home, codex_bin="/nonexistent/codex", force=True), "codex")

    def test_all_snapshot_fixtures(self):
        from datetime import datetime, timedelta

        snap_dir = Path(__file__).resolve().parent / "fixtures" / "snapshots"
        files = sorted(snap_dir.glob("*.json"))
        self.assertTrue(files, "UI snapshot fixtures must exist (milestone 2)")
        for path in files:
            with self.subTest(snapshot=path.name):
                record = json.loads(path.read_text(encoding="utf-8"))
                # Fixtures ship with fixed dates; shift the whole record so
                # the last recent day is today, keeping coverage relative.
                dates = [d["date"] for d in record.get("recentDays", [])]
                if dates:
                    latest = max(datetime.strptime(d, "%Y-%m-%d").date() for d in dates)
                    shift = date.today() - latest
                    if shift.days:
                        def move(value: str) -> str:
                            try:
                                return (datetime.strptime(value, "%Y-%m-%d").date() + shift).isoformat()
                            except ValueError:
                                return value

                        for day in record["recentDays"]:
                            day["date"] = move(day["date"])
                        record["activeDates"] = [move(d) for d in record.get("activeDates", [])]
                check_contract(self, record, path.stem.split("-")[0])


if __name__ == "__main__":
    unittest.main()
