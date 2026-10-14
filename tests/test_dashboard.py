import unittest
from datetime import datetime, timedelta, timezone

from tracker import dashboard
from tracker.inventory import SkillCopy

NOW = datetime(2026, 11, 20, tzinfo=timezone.utc)


def load(skill, source, sid, days_ago, seconds=0):
    stamp = NOW - timedelta(days=days_ago) + timedelta(seconds=seconds)
    return {
        "type": "load",
        "ts": stamp.isoformat(),
        "session_id": sid,
        "skill": skill,
        "source": source,
    }


class DashboardDataTests(unittest.TestCase):
    def test_data_groups_copies_and_builds_consistent_range_summaries(self):
        copies = [
            SkillCopy("active", "C:/private/active/SKILL.md", "personal"),
            SkillCopy("active", "C:/other/active/SKILL.md", "plugin:team/plugin"),
            SkillCopy("idle", "C:/private/idle/SKILL.md", "project"),
        ]
        events = [
            load("active", "agent-read", "s1", 2),
            load("active", "user-slash", "s2", 3),
            load("active", "agent-read", "s2", 3, 20),
            load("active", "skill-tool", "s3", 40),
            load("ghost", "skill-tool", "s4", 5),
            load("ghost", "user-slash", "s5", 4),
        ]

        data = dashboard.build_dashboard_data(events, copies, NOW)
        recent = data["ranges"]["30d"]
        history = data["ranges"]["all"]
        recent_skills = {row["name"]: row for row in recent["skills"]}
        history_skills = {row["name"]: row for row in history["skills"]}

        self.assertEqual(set(recent_skills), {"active", "idle"})
        self.assertEqual(recent_skills["active"]["copyCount"], 2)
        self.assertEqual(recent_skills["active"]["sources"], ["personal", "plugin:team/plugin"])
        self.assertEqual(recent_skills["active"]["counts"], {"agent-read": 1, "user-slash": 1, "skill-tool": 0})
        self.assertEqual(recent_skills["active"]["total"], 2)
        self.assertEqual(recent_skills["active"]["sessions"], 2)
        self.assertTrue(recent_skills["active"]["observed"])
        self.assertFalse(recent_skills["idle"]["observed"])
        self.assertNotIn("path", recent_skills["active"])

        self.assertEqual(recent["summary"], {
            "installedSkills": 2,
            "recordedLoads": 4,
            "observedSkills": 1,
            "notObservedSkills": 1,
        })
        self.assertEqual(history_skills["active"]["counts"]["skill-tool"], 1)
        self.assertEqual(history["summary"]["recordedLoads"], 5)
        self.assertEqual(history["unmatched"], [{
            "name": "ghost",
            "loads": 2,
            "sources": ["user-slash", "skill-tool"],
            "last": (NOW - timedelta(days=4)).isoformat(),
        }])

    def test_renderer_emits_offline_dashboard_controls_and_empty_states(self):
        copies = [SkillCopy("idle", "C:/private/idle/SKILL.md", "personal")]
        html = dashboard.render_html(dashboard.build_dashboard_data([], copies, NOW))

        self.assertIn('id="range-control"', html)
        self.assertIn('data-range="30d"', html)
        self.assertIn('data-range="all"', html)
        self.assertIn('data-filter="all"', html)
        self.assertIn('data-filter="observed"', html)
        self.assertIn('data-filter="not-observed"', html)
        self.assertIn('data-filter="unmatched"', html)
        self.assertIn('selectedRange = button.dataset.range', html)
        self.assertIn('selectedFilter === "observed"', html)
        self.assertIn('selectedFilter === "not-observed"', html)
        self.assertIn('selectedFilter === "unmatched"', html)
        self.assertIn("Last 30 days", html)
        self.assertIn("All recorded history", html)
        self.assertIn("overflow-x:auto", html)
        self.assertIn("No valid load events are recorded", html)
        self.assertIn("No skills were found in the current inventory", html)

    def test_empty_inventory_keeps_unmatched_loads_in_dashboard_data(self):
        data = dashboard.build_dashboard_data([load("ghost", "skill-tool", "s1", 2)], [], NOW)
        view = data["ranges"]["30d"]

        self.assertEqual(view["summary"]["installedSkills"], 0)
        self.assertEqual(view["skills"], [])
        self.assertEqual(view["unmatched"][0]["name"], "ghost")
        self.assertIn("No skills were found in the current inventory", dashboard.render_html(data))

    def test_renderer_escapes_script_like_names_and_omits_paths(self):
        name = '</script><script>alert("x")</script><img src=x onerror=alert(1)>'
        copies = [SkillCopy(name, "C:/private/secret/SKILL.md", "personal")]
        html = dashboard.render_html(dashboard.build_dashboard_data([load(name, "skill-tool", "s1", 1)], copies, NOW))

        self.assertIn(r"\u003c/script\u003e", html)
        self.assertNotIn('</script><script>alert("x")', html)
        self.assertNotIn("<img src=x", html)
        self.assertNotIn("C:/private/secret", html)
        self.assertNotIn("SKILL.md", html)
        self.assertNotIn("<link", html)
        self.assertNotIn('src="http', html.lower())
        self.assertNotIn('href="http', html.lower())
        self.assertIn("textContent", html)
        self.assertNotIn("innerHTML", html)


if __name__ == "__main__":
    unittest.main()