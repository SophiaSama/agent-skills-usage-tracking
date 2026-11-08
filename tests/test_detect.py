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

    def test_ignores_paginated_continuation_via_content_offset(self):
        ev = tool_event("view_file", filePath=SKILL, ContentOffset="46080")
        self.assertIsNone(detect(ev, no_names))

    def test_ignores_paginated_continuation_via_start_line_and_content_offset_camel_case(self):
        ev1 = tool_event("read_file", filePath=SKILL, startLine=50)
        self.assertIsNone(detect(ev1, no_names))
        ev2 = tool_event("view_file", filePath=SKILL, contentOffset=46080)
        self.assertIsNone(detect(ev2, no_names))
        # Initial reads with startLine=1 or contentOffset=0 must NOT be ignored
        ev3 = tool_event("read_file", filePath=SKILL, startLine=1)
        self.assertIsNotNone(detect(ev3, no_names))
        ev4 = tool_event("view_file", filePath=SKILL, contentOffset=0)
        self.assertIsNotNone(detect(ev4, no_names))

    def test_slash_command_case_insensitive_matching(self):
        # Uppercase prompt matching lowercase inventory
        rec1 = detect(prompt_event("/TDD now"), lambda: {"tdd"})
        self.assertIsNotNone(rec1)
        self.assertEqual(rec1["skill"], "tdd")
        self.assertEqual(rec1["source"], "user-slash")

        # Lowercase prompt matching uppercase inventory
        rec2 = detect(prompt_event("/tdd now"), lambda: {"TDD"})
        self.assertIsNotNone(rec2)
        self.assertEqual(rec2["skill"], "TDD")

        # Mixed-case prompt matching mixed-case inventory
        rec3 = detect(prompt_event("/My-Skill do stuff"), lambda: {"My-Skill"})
        self.assertIsNotNone(rec3)
        self.assertEqual(rec3["skill"], "My-Skill")

    def test_skill_tool_with_uppercase_name(self):
        rec = detect(tool_event("Skill", skill="TDD"), no_names)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["skill"], "TDD")
        self.assertEqual(rec["source"], "skill-tool")

    def test_tool_alternate_keys_and_folder_guards(self):
        # camelCase and alternative path keys
        rec_abs = detect(tool_event("read_file", absolutePath=SKILL), no_names)
        self.assertIsNotNone(rec_abs)
        self.assertEqual(rec_abs["skill"], "graphify")

        rec_fp = detect(tool_event("read_file", filepath=SKILL), no_names)
        self.assertIsNotNone(rec_fp)
        self.assertEqual(rec_fp["skill"], "graphify")

        # skill_name and skillName keys
        rec_sn = detect(tool_event("Skill", skill_name="tdd"), no_names)
        self.assertIsNotNone(rec_sn)
        self.assertEqual(rec_sn["skill"], "tdd")

        rec_camel_sn = detect(tool_event("Skill", skillName="tdd"), no_names)
        self.assertIsNotNone(rec_camel_sn)
        self.assertEqual(rec_camel_sn["skill"], "tdd")

        # Directly reading skills/SKILL.md without a skill subdirectory is ignored
        self.assertIsNone(detect(tool_event("read_file", filePath="/path/to/skills/SKILL.md"), no_names))

        # Single quoted argument string cleaning
        rec_single_quote = detect(tool_event("Skill", skill="'quoted-skill'"), no_names)
        self.assertIsNotNone(rec_single_quote)
        self.assertEqual(rec_single_quote["skill"], "quoted-skill")

    def test_slash_with_colon_subcommand_and_case_insensitive_tools(self):
        names = lambda: {"my-skill", "tdd"}
        # Slash with subcommand
        slash_sub = {"hook_event_name": "UserPromptSubmit", "prompt": "/my-skill:run now", "timestamp": TS}
        rec = detect(slash_sub, names)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["skill"], "my-skill")
        self.assertEqual(rec["source"], "user-slash")

        # Case-insensitive tool name "View"
        view_rec = detect(
            {
                "hook_event_name": "PostToolUse",
                "tool_name": "View",
                "tool_input": {"path": "/Users/u/.agents/skills/tdd/SKILL.md"},
                "timestamp": TS,
            },
            names,
        )
        self.assertIsNotNone(view_rec)
        self.assertEqual(view_rec["skill"], "tdd")
        self.assertEqual(view_rec["source"], "agent-read")

        # invocationNum as string "1"
        sess_rec = detect({"conversationId": "conv-42", "invocationNum": "1", "timestamp": TS}, names)
        self.assertIsNotNone(sess_rec)
        self.assertEqual(sess_rec["type"], "session")
        self.assertEqual(sess_rec["session_id"], "conv-42")

        # workspace derived from toolCall.args.Cwd
        ws_rec = detect(
            {
                "conversationId": "conv-42",
                "toolCall": {
                    "name": "view_file",
                    "args": {
                        "AbsolutePath": "/Users/u/.agents/skills/my-skill/SKILL.md",
                        "Cwd": "/Users/u/workspace-project",
                    },
                },
                "timestamp": TS,
            },
            names,
        )
        self.assertIsNotNone(ws_rec)
        self.assertEqual(ws_rec["workspace"], "workspace-project")
        self.assertEqual(ws_rec["skill"], "my-skill")


if __name__ == "__main__":
    unittest.main()


