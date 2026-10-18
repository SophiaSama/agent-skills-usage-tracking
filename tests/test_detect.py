import json
import os
import unittest

from tracker.detect import detect

SKILL = os.path.join("home", "u", ".copilot", "skills", "graphify", "SKILL.md")
CWD = os.path.join("work", "mc_sw")
TS = "2026-10-13T08:00:00.000Z"


def tool_event(tool, **tool_input):
    return {
        "hook_event_name": "PostToolUse",
        "timestamp": TS,
        "session_id": "s1",
        "cwd": CWD,
        "tool_name": tool,
        "tool_input": tool_input,
        "tool_response": "secret text",
    }


def prompt_event(prompt):
    return {
        "hook_event_name": "UserPromptSubmit",
        "timestamp": TS,
        "session_id": "s1",
        "cwd": CWD,
        "prompt": prompt,
    }


def no_names():
    return set()


class DetectTests(unittest.TestCase):
    def test_agent_read_of_skill_file(self):
        rec = detect(tool_event("read_file", filePath=SKILL, startLine=1, endLine=50), no_names)
        self.assertEqual(rec["type"], "load")
        self.assertEqual(rec["skill"], "graphify")
        self.assertEqual(rec["source"], "agent-read")
        self.assertEqual(rec["path"], SKILL)
        self.assertEqual(rec["session_id"], "s1")
        self.assertEqual(rec["workspace"], "mc_sw")
        self.assertEqual(rec["ts"], TS)

    def test_ignores_reads_that_are_not_skill_files(self):
        self.assertIsNone(detect(tool_event("read_file", filePath=os.path.join("work", "README.md")), no_names))
        self.assertIsNone(detect(tool_event("read_file", filePath=os.path.join("work", "docs", "SKILL.md")), no_names))

    def test_ignores_edits_of_skill_file(self):
        self.assertIsNone(detect(tool_event("replace_string_in_file", filePath=SKILL), no_names))

    def test_skill_tool_with_plugin_prefix(self):
        rec = detect(tool_event("Skill", skill="superpowers:brainstorming"), no_names)
        self.assertEqual(rec["skill"], "brainstorming")
        self.assertEqual(rec["source"], "skill-tool")
        self.assertEqual(rec["path"], "")

    def test_slash_command_for_known_skill(self):
        rec = detect(prompt_event("/grill-me plan the thing"), lambda: {"grill-me"})
        self.assertEqual(rec["skill"], "grill-me")
        self.assertEqual(rec["source"], "user-slash")

    def test_plugin_prefixed_slash_command(self):
        rec = detect(prompt_event("/mattpocock:tdd now"), lambda: {"tdd"})
        self.assertEqual(rec["skill"], "tdd")

    def test_unknown_or_non_leading_slash_is_ignored(self):
        self.assertIsNone(detect(prompt_event("/clear"), lambda: {"tdd"}))
        self.assertIsNone(detect(prompt_event("hello /tdd"), lambda: {"tdd"}))

    def test_inventory_not_scanned_for_plain_prompts(self):
        def boom():
            raise AssertionError("inventory scanned")

        self.assertIsNone(detect(prompt_event("just a question"), boom))

    def test_session_start_event(self):
        event = {"hook_event_name": "SessionStart", "timestamp": TS, "session_id": "s1", "cwd": CWD, "source": "new"}
        rec = detect(event, no_names)
        self.assertEqual(rec, {"type": "session", "ts": TS, "session_id": "s1", "workspace": "mc_sw"})

    def test_record_never_contains_prompt_or_response(self):
        text = json.dumps(detect(prompt_event("/tdd topsecret"), lambda: {"tdd"}))
        self.assertNotIn("topsecret", text)
        text = json.dumps(detect(tool_event("read_file", filePath=SKILL), no_names))
        self.assertNotIn("secret text", text)

    def test_malformed_input_is_ignored(self):
        self.assertIsNone(detect({}, no_names))
        bad = {"hook_event_name": "PostToolUse", "tool_name": "read_file", "tool_input": "x"}
        self.assertIsNone(detect(bad, no_names))

    def test_captured_slash_prompt_shape(self):
        rec = detect(prompt_event('/grill-me say only "done"`'), lambda: {"grill-me"})
        self.assertEqual((rec["skill"], rec["source"]), ("grill-me", "user-slash"))

    def test_captured_session_start_shape(self):
        event = {
            "timestamp": TS,
            "hook_event_name": "SessionStart",
            "session_id": "s1",
            "source": "new",
            "model": "auto",
            "cwd": CWD,
        }
        self.assertEqual(detect(event, no_names)["type"], "session")

    def test_captured_windows_skill_read_path(self):
        path = r"C:\Users\uie83312\.agents\skills\grill-me\SKILL.md"
        rec = detect(tool_event("read_file", filePath=path, startLine=1, endLine=300), no_names)
        self.assertEqual((rec["skill"], rec["source"]), ("grill-me", "agent-read"))

    def test_plugin_name_then_skill_name_slash_form(self):
        prompt = "/mattpocock-skills grill-with-docs let's review this plan"
        rec = detect(prompt_event(prompt), lambda: {"grill-with-docs"})
        self.assertEqual((rec["skill"], rec["source"]), ("grill-with-docs", "user-slash"))

    def test_first_token_wins_when_it_is_a_known_skill(self):
        rec = detect(prompt_event("/grill-me tdd"), lambda: {"grill-me", "tdd"})
        self.assertEqual(rec["skill"], "grill-me")

    def test_second_token_is_ignored_when_first_is_not_a_skill(self):
        self.assertIsNone(detect(prompt_event("/clear tdd"), lambda: {"grill-me"}))

    def test_antigravity_session_start_detection(self):
        payload = {
            "conversationId": "conv-antigravity-999",
            "invocationNum": 1,
            "workspacePaths": ["/Users/user/workspace/project-alpha"],
            "timestamp": "2026-10-18T10:00:00Z",
        }
        rec = detect(payload, no_names)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["type"], "session")
        self.assertEqual(rec["session_id"], "conv-antigravity-999")
        self.assertEqual(rec["workspace"], "project-alpha")
        self.assertEqual(rec["agent"], "antigravity")

    def test_antigravity_subsequent_invocation_ignored(self):
        payload = {
            "conversationId": "conv-antigravity-999",
            "invocationNum": 2,
            "workspacePaths": ["/Users/user/workspace/project-alpha"],
            "timestamp": "2026-10-18T10:01:00Z",
        }
        self.assertIsNone(detect(payload, no_names))

    def test_pagination_filtering_across_casing_variants(self):
        self.assertIsNone(detect(tool_event("read_file", filePath=SKILL, startLine=101), no_names))
        self.assertIsNone(detect(tool_event("read_file", filePath=SKILL, start_line=201), no_names))
        self.assertIsNone(detect(tool_event("view_file", AbsolutePath=SKILL, StartLine=51), no_names))
        self.assertIsNone(detect(tool_event("read", path=SKILL, offset=500), no_names))

    def test_claude_xml_command_prompt_normalization(self):
        xml_prompt = "<command-name>/grill-me</command-name><command-args>plan this</command-args>"
        rec = detect(prompt_event(xml_prompt), lambda: {"grill-me"})
        self.assertIsNotNone(rec)
        self.assertEqual((rec["skill"], rec["source"]), ("grill-me", "user-slash"))


if __name__ == "__main__":
    unittest.main()
