import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tracker import dashboard, followthrough
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

    def test_dashboard_integrates_static_quality_followthrough_and_needs_attention(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            healthy_dir = root / "skills" / "healthy-skill"
            (healthy_dir / "references").mkdir(parents=True)
            (healthy_dir / "SKILL.md").write_text(
                "---\nname: healthy-skill\ndescription: Use when running healthy skill checks with references/guide.md.\n---\nRead [guide](references/guide.md).\n",
                encoding="utf-8",
            )
            (healthy_dir / "references" / "guide.md").write_text("guide\n", encoding="utf-8")

            low_ft_dir = root / "skills" / "low-ft-skill"
            (low_ft_dir / "scripts").mkdir(parents=True)
            (low_ft_dir / "SKILL.md").write_text(
                "---\nname: low-ft-skill\ndescription: Use when executing validation scripts inside scripts/check.py.\n---\nRun [check](scripts/check.py).\n",
                encoding="utf-8",
            )
            (low_ft_dir / "scripts" / "check.py").write_text("print('check')\n", encoding="utf-8")

            warn_dir = root / "skills" / "warn-skill"
            warn_dir.mkdir(parents=True)
            (warn_dir / "SKILL.md").write_text("missing frontmatter body\n", encoding="utf-8")

            info_dir = root / "skills" / "info-skill"
            (info_dir / "references").mkdir(parents=True)
            (info_dir / "SKILL.md").write_text(
                "---\nname: info-skill\ndescription: Use when checking info-only unreferenced support file findings.\n---\nMain instructions only.\n",
                encoding="utf-8",
            )
            (info_dir / "references" / "extra.md").write_text("extra\n", encoding="utf-8")

            copies = [
                SkillCopy("healthy-skill", str(healthy_dir / "SKILL.md"), "project", ("claude",)),
                SkillCopy("low-ft-skill", str(low_ft_dir / "SKILL.md"), "project", ("antigravity",)),
                SkillCopy("warn-skill", str(warn_dir / "SKILL.md"), "personal", ("claude",)),
                SkillCopy("info-skill", str(info_dir / "SKILL.md"), "personal", ("codex",)),
            ]

            sessions = []
            # 5 recent Claude sessions for healthy-skill: 4 followed through (80%)
            for idx in range(5):
                ts = NOW - timedelta(days=2 + idx)
                calls = [
                    followthrough.ToolCall(
                        ts.isoformat(), "Read", {"file_path": str(healthy_dir / "SKILL.md")}
                    )
                ]
                if idx < 4:
                    calls.append(
                        followthrough.ToolCall(
                            ts.isoformat(),
                            "Read",
                            {"file_path": str(healthy_dir / "references" / "guide.md")},
                        )
                    )
                sessions.append(followthrough.Session("claude", f"cl-{idx}", ts, calls))

            # 5 recent Antigravity sessions for low-ft-skill: 1 followed through (20% < 30%)
            for idx in range(5):
                ts = NOW - timedelta(days=1 + idx)
                calls = [
                    followthrough.ToolCall(
                        ts.isoformat(), "read_file", {"Path": str(low_ft_dir / "SKILL.md")}
                    )
                ]
                if idx == 0:
                    calls.append(
                        followthrough.ToolCall(
                            ts.isoformat(),
                            "run_command",
                            {"CommandLine": f"python3 {low_ft_dir / 'scripts' / 'check.py'}"},
                        )
                    )
                sessions.append(followthrough.Session("antigravity", f"ag-{idx}", ts, calls))

            # 1 older Antigravity session (45 days ago) that followed through -> visible in 'all' (2/6 = 33.3% >= 30%)
            old_ts = NOW - timedelta(days=45)
            sessions.append(
                followthrough.Session(
                    "antigravity",
                    "ag-old",
                    old_ts,
                    [
                        followthrough.ToolCall(
                            old_ts.isoformat(), "read_file", {"Path": str(low_ft_dir / "SKILL.md")}
                        ),
                        followthrough.ToolCall(
                            old_ts.isoformat(),
                            "run_command",
                            {"CommandLine": f"python3 {low_ft_dir / 'scripts' / 'check.py'}"},
                        ),
                    ],
                )
            )

            events = [load("healthy-skill", "agent-read", "cl-0", 2)]
            data = dashboard.build_dashboard_data(events, copies, NOW, sessions=sessions)

            recent = data["ranges"]["30d"]
            history = data["ranges"]["all"]
            self.assertEqual(
                recent["summary"],
                {
                    "installedSkills": 4,
                    "recordedLoads": 1,
                    "observedSkills": 1,
                    "notObservedSkills": 3,
                },
            )
            self.assertEqual(
                recent["qualitySummary"],
                {
                    "auditedSkills": 4,
                    "cleanSkills": 2,
                    "warningSkills": 1,
                    "infoOnlySkills": 1,
                    "scoredFollowSkills": 2,
                    "lowFollowSkills": 1,
                    "needsAttentionSkills": 2,
                },
            )

            by_name_30d = {row["name"]: row for row in recent["skills"]}
            self.assertTrue(by_name_30d["healthy-skill"]["quality"]["ok"])
            self.assertEqual(by_name_30d["healthy-skill"]["quality"]["status"], "clean")
            self.assertEqual(by_name_30d["healthy-skill"]["followthrough"]["rate"], 0.8)
            self.assertFalse(by_name_30d["healthy-skill"]["needsAttention"])

            self.assertTrue(by_name_30d["low-ft-skill"]["followthrough"]["low"])
            self.assertEqual(by_name_30d["low-ft-skill"]["followthrough"]["rate"], 0.2)
            self.assertTrue(by_name_30d["low-ft-skill"]["needsAttention"])

            # In all-history, low-ft-skill is 2/6 = 33.33% (>= 30%), so no longer flagged low!
            by_name_all = {row["name"]: row for row in history["skills"]}
            self.assertFalse(by_name_all["low-ft-skill"]["followthrough"]["low"])
            self.assertFalse(by_name_all["low-ft-skill"]["needsAttention"])

            self.assertEqual(by_name_30d["warn-skill"]["quality"]["status"], "warning")
            self.assertIn("missing-frontmatter", by_name_30d["warn-skill"]["quality"]["issueCodes"])
            self.assertTrue(by_name_30d["warn-skill"]["needsAttention"])

            self.assertEqual(by_name_30d["info-skill"]["quality"]["status"], "info")
            self.assertIn("unreferenced-file", by_name_30d["info-skill"]["quality"]["issueCodes"])
            self.assertFalse(by_name_30d["info-skill"]["needsAttention"])

            # Per-agent scoping: Antigravity view only has low-ft-skill
            agy_30d = data["agents"]["antigravity"]["30d"]
            self.assertEqual([r["name"] for r in agy_30d["skills"]], ["low-ft-skill"])
            self.assertEqual(agy_30d["qualitySummary"]["lowFollowSkills"], 1)

            html = dashboard.render_html(data)
            self.assertIn('data-filter="needs-attention"', html)
            self.assertIn('selectedFilter === "needs-attention"', html)
            self.assertIn("Follow-through", html)
            self.assertIn("Quality", html)
            self.assertIn("missing-frontmatter", html)
            self.assertNotIn(str(root), html)
            self.assertNotIn("SKILL.md", html)


if __name__ == "__main__":
    unittest.main()