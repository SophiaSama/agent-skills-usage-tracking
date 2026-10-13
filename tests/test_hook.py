import json
import tempfile
import unittest
from pathlib import Path

import hook
from tracker import store

TS = "2026-10-13T08:00:00.000Z"


def make_skill(root: Path, *parts):
    folder = root.joinpath(*parts)
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("x", encoding="utf-8")
    return folder / "SKILL.md"


def payload(**fields):
    base = {"timestamp": TS, "session_id": "s1", "cwd": str(Path("work") / "mc_sw")}
    base.update(fields)
    return json.dumps(base).encode("utf-8")


class HookTests(unittest.TestCase):
    def test_records_agent_read_of_skill_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "data"
            skill = make_skill(Path(tmp), "home", ".copilot", "skills", "graphify")
            raw = payload(
                hook_event_name="PostToolUse",
                tool_name="read_file",
                tool_input={"filePath": str(skill)},
                tool_response="x",
            )
            hook.run(raw, data_dir=base, home=Path(tmp) / "home")
            events = store.read_events(base)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["skill"], "graphify")
            self.assertEqual(events[0]["source"], "agent-read")

    def test_records_slash_command_using_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "data"
            make_skill(Path(tmp), "home", ".agents", "skills", "tdd")
            hook.run(
                payload(hook_event_name="UserPromptSubmit", prompt="/tdd go"),
                data_dir=base,
                home=Path(tmp) / "home",
            )
            events = store.read_events(base)
            self.assertEqual([(e["skill"], e["source"]) for e in events], [("tdd", "user-slash")])

    def test_records_session_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "data"
            hook.run(payload(hook_event_name="SessionStart", source="new"), data_dir=base, home=Path(tmp))
            self.assertEqual([e["type"] for e in store.read_events(base)], ["session"])

    def test_unrelated_tool_calls_record_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "data"
            raw = payload(hook_event_name="PostToolUse", tool_name="run_in_terminal", tool_input={"command": "ls"})
            hook.run(raw, data_dir=base, home=Path(tmp))
            self.assertEqual(store.read_events(base), [])

    def test_bad_input_logs_only_the_error_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "data"
            hook.run(b"not json {topsecret}", data_dir=base, home=Path(tmp))
            self.assertEqual(store.read_events(base), [])
            log = (base / "hook-errors.log").read_text(encoding="utf-8")
            self.assertIn("JSONDecodeError", log)
            self.assertNotIn("topsecret", log)


if __name__ == "__main__":
    unittest.main()
