import contextlib
import io
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
