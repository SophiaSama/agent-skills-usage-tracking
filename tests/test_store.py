import tempfile
import unittest
from pathlib import Path

from tracker import store


class StoreTests(unittest.TestCase):
    def test_events_go_to_monthly_files_and_read_back_in_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store.append_event({"type": "load", "ts": "2026-09-30T23:59:00Z", "skill": "a"}, base)
            store.append_event({"type": "load", "ts": "2026-10-01T00:01:00Z", "skill": "b"}, base)
            self.assertTrue((base / "events-2026-09.jsonl").is_file())
            self.assertTrue((base / "events-2026-10.jsonl").is_file())
            self.assertEqual([e["skill"] for e in store.read_events(base)], ["a", "b"])

    def test_malformed_and_non_object_lines_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "events-2026-10.jsonl").write_text(
                'not json\n{"type":"session","ts":"2026-10-01T00:00:00Z"}\n[1]\n', encoding="utf-8"
            )
            events = store.read_events(base)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["type"], "session")

    def test_undecodable_bytes_do_not_abort_the_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "events-2026-10.jsonl").write_bytes(
                b'\xff\xfe broken\n{"type":"session","ts":"2026-10-01T00:00:00Z"}\n'
            )
            self.assertEqual([e["type"] for e in store.read_events(base)], ["session"])

    def test_missing_directory_reads_as_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(store.read_events(Path(tmp) / "nope"), [])


if __name__ == "__main__":
    unittest.main()
