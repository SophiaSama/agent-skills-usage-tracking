import contextlib
import csv
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import skill_usage
from tracker import store


class CliTests(unittest.TestCase):
    def test_report_prints_markdown_and_writes_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "data"
            store.append_event(
                {"type": "session", "ts": "2026-10-13T08:00:00Z", "session_id": "s1", "workspace": "w"}, base
            )
            csv_path = Path(tmp) / "out.csv"
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = skill_usage.main(
                    ["report", "--data-dir", str(base), "--workspace", tmp, "--csv", str(csv_path)]
                )
            self.assertEqual(code, 0)
            self.assertIn("# Copilot skill load report", buffer.getvalue())
            self.assertTrue(csv_path.is_file())

    def test_dashboard_writes_requested_output_using_multiple_workspaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            workspace_a = root / "workspace-a"
            workspace_b = root / "workspace-b"
            for workspace, name in ((workspace_a, "alpha"), (workspace_b, "beta")):
                skill = workspace / ".agents" / "skills" / name
                skill.mkdir(parents=True)
                (skill / "SKILL.md").write_text("private skill contents", encoding="utf-8")
            data_dir = root / "events"
            store.append_event(
                {
                    "type": "load",
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "session_id": "s1",
                    "skill": "alpha",
                    "source": "skill-tool",
                },
                data_dir,
            )
            output = root / "reports" / "dashboard.html"
            output.parent.mkdir()

            with patch("skill_usage.Path.home", return_value=home):
                code = skill_usage.main(
                    [
                        "dashboard",
                        "--data-dir",
                        str(data_dir),
                        "--workspace",
                        str(workspace_a),
                        "--workspace",
                        str(workspace_b),
                        "--output",
                        str(output),
                    ]
                )

            html = output.read_text(encoding="utf-8")
            self.assertEqual(code, 0)
            self.assertIn("alpha", html)
            self.assertIn("beta", html)
            self.assertIn("Copilot Skill Usage Dashboard", html)
            self.assertNotIn("private skill contents", html)

    def test_dashboard_uses_default_output_in_current_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            workspace_skill = root / ".agents" / "skills" / "local-skill"
            workspace_skill.mkdir(parents=True)
            (workspace_skill / "SKILL.md").write_text("metadata only", encoding="utf-8")
            data_dir = root / "events"

            with patch("skill_usage.Path.home", return_value=home), patch(
                "skill_usage.Path.cwd", return_value=root
            ):
                code = skill_usage.main(["dashboard", "--data-dir", str(data_dir)])

            output = root / "skill-usage-dashboard.html"
            self.assertEqual(code, 0)
            self.assertTrue(output.is_file())
            self.assertIn("local-skill", output.read_text(encoding="utf-8"))

    def test_dashboard_reports_output_write_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stderr = io.StringIO()
            output = root / "missing" / "dashboard.html"
            with patch("skill_usage.Path.home", return_value=root / "home"), contextlib.redirect_stderr(stderr):
                code = skill_usage.main(["dashboard", "--output", str(output)])

            self.assertNotEqual(code, 0)
            self.assertIn(str(output), stderr.getvalue())

    def test_maybe_sync_skips_when_copilot_filter_specified(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            with patch("skill_usage.Path.home", return_value=home), patch(
                "tracker.sync.sync_events"
            ) as mock_sync:
                code = skill_usage.main(["report", "--agent", "copilot", "--workspace", tmp])
                self.assertEqual(code, 0)
                mock_sync.assert_not_called()
    def test_report_csv_creates_missing_parent_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "data"
            store.append_event(
                {"type": "session", "ts": "2026-10-13T08:00:00Z", "session_id": "s1", "workspace": "w"}, base
            )
            csv_path = Path(tmp) / "nested" / "subfolder" / "out.csv"
            code = skill_usage.main(
                ["report", "--data-dir", str(base), "--workspace", tmp, "--csv", str(csv_path), "--no-sync"]
            )
            self.assertEqual(code, 0)
            self.assertTrue(csv_path.is_file())

    def test_report_csv_reports_write_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "data"
            store.append_event(
                {"type": "session", "ts": "2026-10-13T08:00:00Z", "session_id": "s1", "workspace": "w"}, base
            )
            # Create a regular file where directory is expected to cause OSError
            blocker = Path(tmp) / "blocker"
            blocker.write_text("file")
            csv_path = blocker / "out.csv"
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = skill_usage.main(
                    ["report", "--data-dir", str(base), "--workspace", tmp, "--csv", str(csv_path), "--no-sync"]
                )
            self.assertEqual(code, 1)
            self.assertIn("error: could not write CSV to", stderr.getvalue())

    def test_followthrough_prints_report_and_writes_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            ws = root / "workspace"
            skill_dir = ws / ".agents" / "skills" / "deploy-helper"
            (skill_dir / "scripts").mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: deploy-helper\ndescription: Use when running deployment checks with scripts/verify.py.\n---\nRun scripts/verify.py.\n",
                encoding="utf-8",
            )
            (skill_dir / "scripts" / "verify.py").write_text("print('ok')\n", encoding="utf-8")

            transcript = home / ".gemini" / "antigravity" / "brain" / "conv-1" / ".system_generated" / "logs" / "transcript.jsonl"
            transcript.parent.mkdir(parents=True)
            now_iso = datetime.now(timezone.utc).isoformat()
            steps = [
                {
                    "type": "PLANNER_RESPONSE",
                    "created_at": now_iso,
                    "tool_calls": [
                        {"name": "read_file", "args": {"Path": str(skill_dir / "SKILL.md")}},
                        {"name": "run_command", "args": {"CommandLine": f"python3 {skill_dir / 'scripts' / 'verify.py'}"}},
                    ],
                }
            ]
            transcript.write_text("\n".join(json.dumps(s) for s in steps) + "\n", encoding="utf-8")

            csv_path = root / "nested" / "ft" / "followthrough.csv"
            out = io.StringIO()
            with patch("skill_usage.Path.home", return_value=home), contextlib.redirect_stdout(out):
                code = skill_usage.main(
                    [
                        "followthrough",
                        "--days",
                        "30",
                        "--workspace",
                        str(ws),
                        "--agent",
                        "antigravity",
                        "--csv",
                        str(csv_path),
                    ]
                )

            self.assertEqual(code, 0)
            rendered = out.getvalue()
            self.assertIn("# Skill follow-through report", rendered)
            self.assertIn("deploy-helper", rendered)
            self.assertNotIn(str(root), rendered)
            self.assertTrue(csv_path.is_file())
            with open(csv_path, encoding="utf-8", newline="") as fh:
                rows = list(csv.DictReader(fh))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["skill"], "deploy-helper")
            self.assertEqual(rows[0]["sessions_scored"], "1")
            self.assertEqual(rows[0]["sessions_followed"], "1")

    def test_followthrough_csv_reports_write_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            blocker = root / "blocker"
            blocker.write_text("file", encoding="utf-8")
            csv_path = blocker / "ft.csv"
            out = io.StringIO()
            err = io.StringIO()
            with patch("skill_usage.Path.home", return_value=home), contextlib.redirect_stdout(
                out
            ), contextlib.redirect_stderr(err):
                code = skill_usage.main(["followthrough", "--workspace", str(root), "--csv", str(csv_path)])
            self.assertEqual(code, 1)
            self.assertIn("error: could not write CSV to", err.getvalue())

    def test_audit_prints_markdown_supports_issues_only_and_writes_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            ws = root / "workspace"
            clean_dir = ws / ".agents" / "skills" / "clean-skill"
            clean_dir.mkdir(parents=True)
            (clean_dir / "SKILL.md").write_text(
                "---\nname: clean-skill\ndescription: Use when validating static skill quality checks across local workspaces.\n---\n# Clean Skill\n",
                encoding="utf-8",
            )
            broken_dir = ws / ".agents" / "skills" / "broken-skill"
            broken_dir.mkdir(parents=True)
            (broken_dir / "SKILL.md").write_text(
                "---\nname: broken-skill\n---\nSee [missing guide](references/missing.md).\n",
                encoding="utf-8",
            )

            csv_path = root / "nested" / "audit" / "quality.csv"
            full_out = io.StringIO()
            with patch("skill_usage.Path.home", return_value=home), contextlib.redirect_stdout(full_out):
                code = skill_usage.main(["audit", "--workspace", str(ws), "--csv", str(csv_path)])

            self.assertEqual(code, 0)
            full_md = full_out.getvalue()
            self.assertIn("# Skill quality audit report", full_md)
            self.assertIn("broken-skill", full_md)
            self.assertIn("missing-description", full_md)
            self.assertIn("broken-link", full_md)
            self.assertIn("clean-skill", full_md)
            self.assertNotIn(str(root), full_md)
            self.assertTrue(csv_path.is_file())
            with open(csv_path, encoding="utf-8", newline="") as fh:
                rows = {r["skill"]: r for r in csv.DictReader(fh)}
            self.assertEqual(rows["broken-skill"]["status"], "warning")
            self.assertEqual(rows["clean-skill"]["status"], "clean")

            issues_out = io.StringIO()
            with patch("skill_usage.Path.home", return_value=home), contextlib.redirect_stdout(issues_out):
                code_issues = skill_usage.main(["audit", "--workspace", str(ws), "--issues-only"])
            self.assertEqual(code_issues, 0)
            issues_md = issues_out.getvalue()
            self.assertIn("broken-skill", issues_md)
            self.assertNotIn("## Clean skills", issues_md)
            self.assertNotIn("clean-skill", issues_md)

    def test_audit_csv_reports_write_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            blocker = root / "blocker"
            blocker.write_text("file", encoding="utf-8")
            csv_path = blocker / "audit.csv"
            out = io.StringIO()
            err = io.StringIO()
            with patch("skill_usage.Path.home", return_value=home), contextlib.redirect_stdout(
                out
            ), contextlib.redirect_stderr(err):
                code = skill_usage.main(["audit", "--workspace", str(root), "--csv", str(csv_path)])
            self.assertEqual(code, 1)
            self.assertIn("error: could not write CSV to", err.getvalue())

    def test_dashboard_cli_embeds_quality_and_followthrough_without_leaking_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            ws = root / "workspace"
            skill_dir = ws / ".agents" / "skills" / "needs-help"
            (skill_dir / "references").mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("no frontmatter here\n", encoding="utf-8")
            (skill_dir / "references" / "doc.md").write_text("guide\n", encoding="utf-8")

            now_iso = datetime.now(timezone.utc).isoformat()
            for idx in range(5):
                tpath = (
                    home
                    / ".gemini"
                    / "antigravity"
                    / "brain"
                    / f"conv-{idx}"
                    / ".system_generated"
                    / "logs"
                    / "transcript.jsonl"
                )
                tpath.parent.mkdir(parents=True)
                step = {
                    "type": "PLANNER_RESPONSE",
                    "created_at": now_iso,
                    "tool_calls": [{"name": "read_file", "args": {"Path": str(skill_dir / "SKILL.md")}}],
                }
                tpath.write_text(json.dumps(step) + "\n", encoding="utf-8")

            data_dir = root / "events"
            output = root / "dashboard.html"
            out = io.StringIO()
            with patch("skill_usage.Path.home", return_value=home), contextlib.redirect_stdout(out):
                code = skill_usage.main(
                    [
                        "dashboard",
                        "--no-sync",
                        "--data-dir",
                        str(data_dir),
                        "--workspace",
                        str(ws),
                        "--output",
                        str(output),
                    ]
                )

            self.assertEqual(code, 0)
            html = output.read_text(encoding="utf-8")
            self.assertIn('data-filter="needs-attention"', html)
            self.assertIn("missing-frontmatter", html)
            self.assertIn("low-followthrough", html)
            self.assertNotIn(str(root), html)
            self.assertNotIn("SKILL.md", html)


if __name__ == "__main__":
    unittest.main()

