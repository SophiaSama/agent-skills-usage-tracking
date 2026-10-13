import csv
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tracker import report
from tracker.inventory import SkillCopy

NOW = datetime(2026, 11, 20, tzinfo=timezone.utc)

COPIES = [
    SkillCopy("used", "/p/used/SKILL.md", "personal"),
    SkillCopy("idle", "/p/idle/SKILL.md", "personal"),
    SkillCopy("dup", "/a/dup/SKILL.md", "personal"),
    SkillCopy("dup", "/b/dup/SKILL.md", "plugin:m/p"),
]


def ts(days_ago):
    return (NOW - timedelta(days=days_ago)).isoformat()


def load(skill, source, sid, days_ago):
    return {
        "type": "load",
        "ts": ts(days_ago),
        "session_id": sid,
        "workspace": "w",
        "skill": skill,
        "path": "",
        "source": source,
    }


def session(sid, days_ago):
    return {"type": "session", "ts": ts(days_ago), "session_id": sid, "workspace": "w"}


def rows_by_name(summary):
    return {row.name: row for row in summary.rows}


def enough_events():
    events = [session("old", 40)]
    events += [session(f"s{i}", 1 + i) for i in range(25)]
    events += [load("used", "agent-read", "s1", 2), load("ghost", "skill-tool", "s2", 3)]
    return events


class ReportTests(unittest.TestCase):
    def test_counts_by_source_with_sessions_and_last_load(self):
        events = [
            load("used", "agent-read", "s1", 5),
            load("used", "agent-read", "s1", 4),
            load("used", "user-slash", "s2", 6),
        ]
        row = rows_by_name(report.summarize(events, COPIES, NOW, 30))["used"]
        self.assertEqual(row.counts, {"agent-read": 2, "user-slash": 1, "skill-tool": 0})
        self.assertEqual(row.total, 3)
        self.assertEqual(row.sessions, {"s1", "s2"})
        self.assertEqual(row.last, NOW - timedelta(days=4))

    def test_insufficient_data_is_not_called_review_candidate(self):
        events = [session("s1", 3), load("used", "agent-read", "s1", 2)]
        summary = report.summarize(events, COPIES, NOW, 30)
        text = report.render(summary, 30, 20)
        self.assertIn("Insufficient data", text)
        self.assertIn("## Not observed (insufficient data)", text)
        self.assertNotIn("Review candidates", text)
        self.assertIn("idle", text)

    def test_enough_coverage_lists_review_candidates(self):
        summary = report.summarize(enough_events(), COPIES, NOW, 30)
        self.assertTrue(report.meets_threshold(summary, 30, 20))
        text = report.render(summary, 30, 20)
        self.assertIn("## Review candidates (not observed)", text)
        self.assertIn("- idle (personal)", text)

    def test_short_window_never_meets_threshold(self):
        summary = report.summarize(enough_events(), COPIES, NOW, 7)
        self.assertFalse(report.meets_threshold(summary, 30, 20))

    def test_duplicate_names_list_every_copy(self):
        summary = report.summarize(enough_events(), COPIES, NOW, 30)
        text = report.render(summary, 30, 20)
        self.assertIn("## Duplicate names", text)
        self.assertIn("/a/dup/SKILL.md (personal)", text)
        self.assertIn("/b/dup/SKILL.md (plugin:m/p)", text)

    def test_loads_without_inventory_entry_are_unmatched(self):
        summary = report.summarize(enough_events(), COPIES, NOW, 30)
        self.assertEqual(rows_by_name(summary)["ghost"].copies, [])
        self.assertIn("- ghost: 1 loads", report.render(summary, 30, 20))

    def test_csv_status_values(self):
        summary = report.summarize(enough_events(), COPIES, NOW, 30)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "r.csv"
            report.write_csv(summary, out, True)
            with open(out, newline="", encoding="utf-8") as fh:
                status = {row["skill"]: row["status"] for row in csv.DictReader(fh)}
        self.assertEqual(
            status,
            {"used": "loaded", "idle": "review-candidate", "dup": "review-candidate", "ghost": "unmatched"},
        )


if __name__ == "__main__":
    unittest.main()
