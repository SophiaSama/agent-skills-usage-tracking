import contextlib
import io
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


if __name__ == "__main__":
    unittest.main()
