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


def load_at(skill, source, sid, seconds):
    base = NOW - timedelta(days=1)
    event = load(skill, source, sid, 0)
    event["ts"] = (base + timedelta(seconds=seconds)).isoformat()
    return event


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
    def test_slash_and_the_agent_read_that_follows_count_once(self):
        events = [load_at("used", "user-slash", "s1", 0), load_at("used", "agent-read", "s1", 5)]
        summary = report.summarize(events, COPIES, NOW, 30)
        row = rows_by_name(summary)["used"]
        self.assertEqual(row.counts, {"agent-read": 0, "user-slash": 1, "skill-tool": 0})
        self.assertEqual(row.total, 1)
        self.assertEqual(summary.merged, 1)
        self.assertIn("merged into the slash load that triggered them", report.render(summary, 30, 20))

    def test_read_after_the_pairing_window_is_counted_separately(self):
        events = [load_at("used", "user-slash", "s1", 0), load_at("used", "agent-read", "s1", 121)]
        row = rows_by_name(report.summarize(events, COPIES, NOW, 30))["used"]
        self.assertEqual(row.counts["agent-read"], 1)

    def test_read_in_another_session_or_for_another_skill_is_not_merged(self):
        events = [
            load_at("used", "user-slash", "s1", 0),
            load_at("used", "agent-read", "s2", 5),
            load_at("idle", "agent-read", "s1", 5),
        ]
        summary = report.summarize(events, COPIES, NOW, 30)
        self.assertEqual(summary.merged, 0)
        self.assertEqual(rows_by_name(summary)["used"].counts["agent-read"], 1)

    def test_one_slash_absorbs_only_the_first_following_read(self):
        events = [
            load_at("used", "user-slash", "s1", 0),
            load_at("used", "agent-read", "s1", 5),
            load_at("used", "agent-read", "s1", 30),
        ]
        row = rows_by_name(report.summarize(events, COPIES, NOW, 30))["used"]
        self.assertEqual(row.counts, {"agent-read": 1, "user-slash": 1, "skill-tool": 0})

    def test_read_before_the_slash_is_not_merged(self):
        events = [load_at("used", "agent-read", "s1", 0), load_at("used", "user-slash", "s1", 5)]
        row = rows_by_name(report.summarize(events, COPIES, NOW, 30))["used"]
        self.assertEqual(row.counts["agent-read"], 1)

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

    def test_all_history_includes_old_loads_and_preserves_pairing(self):
        events = [
            load("used", "user-slash", "s1", 40),
            load("used", "agent-read", "s1", 40),
            load("used", "skill-tool", "s2", 35),
            {**load("used", "agent-read", "s3", 50), "ts": "invalid"},
        ]

        history = report.summarize_all(events, COPIES, NOW)
        history_row = rows_by_name(history)["used"]
        self.assertEqual(history_row.counts, {"agent-read": 0, "user-slash": 1, "skill-tool": 1})
        self.assertEqual(history_row.sessions, {"s1", "s2"})
        self.assertEqual(history_row.last, NOW - timedelta(days=35))
        self.assertEqual(history.merged, 1)

        recent = report.summarize(events, COPIES, NOW, 30)
        self.assertEqual(rows_by_name(recent)["used"].total, 0)

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

    def test_duplicate_names_are_one_line_each_without_paths(self):
        summary = report.summarize(enough_events(), COPIES, NOW, 30)
        text = report.render(summary, 30, 20)
        self.assertIn("## Duplicate names", text)
        self.assertIn("- dup (2 copies: personal, plugin:m/p)", text)
        self.assertNotIn("/a/dup/SKILL.md", text)

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
                rows = {row["skill"]: row for row in csv.DictReader(fh)}
        status = {name: row["status"] for name, row in rows.items()}
        self.assertEqual(
            status,
            {"used": "loaded", "idle": "review-candidate", "dup": "review-candidate", "ghost": "unmatched"},
        )
        self.assertEqual(rows["dup"]["paths"], "/a/dup/SKILL.md;/b/dup/SKILL.md")

    def test_report_render_multi_agent_breakdown(self):
        first = datetime(2026, 10, 1, tzinfo=timezone.utc)
        last = datetime(2026, 10, 15, tzinfo=timezone.utc)
        row = report.Row(
            name="skill-a",
            counts={"agent-read": 5, "user-slash": 0, "skill-tool": 0},
            sessions={"s1", "s2", "s3"},
            last=last,
            agents={"copilot", "antigravity"},
            by_agent={"copilot": 2, "antigravity": 3},
        )
        summary = report.Summary(
            days=30,
            first=first,
            coverage_days=15,
            window_days=15,
            sessions={"s1", "s2", "s3"},
            rows=[row],
            merged=0,
            agent="all",
            by_agent={"copilot": 2, "antigravity": 3},
        )
        rendered = report.render(summary, min_days=10, min_sessions=2)
        self.assertIn("# All agents skill load report", rendered)
        self.assertIn("- Loads by agent: Antigravity: 3, Copilot: 2", rendered)


if __name__ == "__main__":
    unittest.main()
