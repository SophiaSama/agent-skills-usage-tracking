import contextlib
import csv
import io
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import skill_usage
from tracker import followthrough as ft
from tracker import inventory

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)


def touch(path: Path, text: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def read(path) -> ft.ToolCall:
    return ft.ToolCall("2026-11-24T10:00:00Z", "read_file", {"Path": str(path)})


def run(cmd) -> ft.ToolCall:
    return ft.ToolCall("2026-11-24T10:00:00Z", "run_command", {"CommandLine": cmd})


class FollowThroughTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        skills = self.home / ".gemini" / "config" / "skills"
        self.rich = skills / "rich"
        touch(self.rich / "SKILL.md")
        touch(self.rich / "scripts" / "go.py")
        touch(self.rich / "references" / "api.md")
        self.bare = skills / "bare"
        touch(self.bare / "SKILL.md")
        self.copies = inventory.scan(self.home, [], agent="antigravity")

    def tearDown(self):
        self.tmp.cleanup()

    def session(self, *calls, sid="s1", agent="antigravity", ts=NOW):
        return ft.Session(agent, sid, ts, list(calls))

    def rows(self, *sessions, **kw):
        return {r.name: r for r in ft.analyze(sessions, self.copies, NOW, **kw)}

    def test_follow_agents_covers_all_five_transcript_agents(self):
        self.assertEqual(ft.FOLLOW_AGENTS, ("antigravity", "claude", "pi", "hermes", "codex"))

    def test_support_files_detected_and_skill_md_alone_is_not_support(self):
        support = ft.support_map(self.copies)
        self.assertTrue(support["rich"])
        self.assertFalse(support["bare"])

    def test_metadata_files_are_not_support(self):
        for rel in ("agents/openai.yaml", "LICENSE.txt", "README.md", "CHANGELOG.md", "package.json", "pyproject.toml", "plugin.json", "manifest.json"):
            touch(self.bare / rel)
        self.assertFalse(ft.has_support_files(str(self.bare / "SKILL.md")))
        touch(self.bare / "notes.md")
        self.assertTrue(ft.has_support_files(str(self.bare / "SKILL.md")))

    def test_metadata_files_excluded_in_follow_kind(self):
        metadata_paths = [
            self.rich / "SKILL.md",
            self.rich / "agents" / "openai.yaml",
            self.rich / "README.md",
            self.rich / "README",
            self.rich / "LICENSE",
            self.rich / "LICENSE.txt",
            self.rich / "CHANGELOG.md",
            self.rich / "package.json",
            self.rich / "pyproject.toml",
            self.rich / "plugin.json",
            self.rich / "manifest.json",
        ]
        for meta in metadata_paths:
            self.assertIsNone(ft.follow_kind(read(meta), "rich"), f"expected None for read({meta})")
            self.assertIsNone(ft.follow_kind(run(f"cat {meta}"), "rich"), f"expected None for run({meta})")

        # Command touching both metadata and a real script still detects the script
        combined = run(f"cat {self.rich}/README.md && python3 {self.rich}/scripts/go.py")
        self.assertEqual(ft.follow_kind(combined, "rich"), "script")

    def test_skill_view_follow_through_detection(self):
        # Loading SKILL.md via skill_view (no file_path) is a load, not follow-through
        self.assertIsNone(ft.follow_kind(ft.ToolCall(None, "skill_view", {"name": "rich"}), "rich"))
        self.assertIsNone(ft.follow_kind(ft.ToolCall(None, "skill_view", {"name": "rich", "file_path": ""}), "rich"))
        self.assertIsNone(ft.follow_kind(ft.ToolCall(None, "skill_view", {"name": "rich", "file_path": "SKILL.md"}), "rich"))

        # Metadata via skill_view is not follow-through
        for meta_rel in ("agents/openai.yaml", "README.md", "LICENSE", "CHANGELOG.md", "package.json", "pyproject.toml", "plugin.json", "manifest.json"):
            self.assertIsNone(
                ft.follow_kind(ft.ToolCall(None, "skill_view", {"name": "rich", "file_path": meta_rel}), "rich"),
                f"expected None for skill_view {meta_rel}",
            )

        # Different skill name does not match
        self.assertIsNone(
            ft.follow_kind(ft.ToolCall(None, "skill_view", {"name": "other", "file_path": "references/api.md"}), "rich")
        )

        # Reference vs script across all SKILL_NAME_KEYS and namespaced tool names
        for key in ("skill", "name", "skill_name", "skillName"):
            ref_call = ft.ToolCall(None, "skill_view", {key: "rich", "file_path": "references/api.md"})
            self.assertEqual(ft.follow_kind(ref_call, "rich"), "reference")
            script_call = ft.ToolCall(None, "hermes:skill_view", {key: "RICH", "file_path": "scripts/go.py"})
            self.assertEqual(ft.follow_kind(script_call, "rich"), "script")

        # End-to-end analyze with skill_view load followed by skill_view reference read
        r = self.rows(
            self.session(
                ft.ToolCall(None, "skill_view", {"name": "rich"}),
                ft.ToolCall(None, "skill_view", {"name": "rich", "file_path": "references/api.md"}),
            )
        )["rich"]
        self.assertEqual((r.sessions_loaded, r.sessions_scored, r.sessions_followed), (1, 1, 1))
        self.assertEqual(r.kinds, {"reference": 1, "script": 0})

    def test_prompt_slash_load_counts_follow_through_and_never_self_triggers(self):
        # User prompt mentioning a reference path must NOT self-trigger follow-through without an agent tool call
        s_unfollowed = self.session(
            ft.prompt_call(f"/rich check {self.rich}/references/api.md", "2026-11-24T10:00:00Z"),
            sid="u1",
        )
        r1 = self.rows(s_unfollowed)["rich"]
        self.assertEqual((r1.sessions_loaded, r1.sessions_scored, r1.sessions_followed), (1, 1, 0))

        # User slash load followed by agent reading reference counts as follow-through
        s_followed = self.session(
            ft.prompt_call("/rich design this", "2026-11-24T10:00:00Z"),
            read(self.rich / "references" / "api.md"),
            sid="u2",
        )
        r2 = self.rows(s_followed)["rich"]
        self.assertEqual((r2.sessions_loaded, r2.sessions_scored, r2.sessions_followed), (1, 1, 1))
        self.assertEqual(r2.kinds["reference"], 1)

    def test_reference_read_after_load_counts_as_follow_through(self):
        r = self.rows(self.session(read(self.rich / "SKILL.md"), read(self.rich / "references" / "api.md")))["rich"]
        self.assertEqual((r.sessions_loaded, r.sessions_followed), (1, 1))
        self.assertEqual(r.kinds, {"reference": 1, "script": 0})
        self.assertEqual(r.rate, 1.0)

    def test_running_a_skill_script_counts_as_script(self):
        r = self.rows(self.session(read(self.rich / "SKILL.md"), run(f"python3 {self.rich}/scripts/go.py --x 1")))["rich"]
        self.assertEqual(r.kinds["script"], 1)

    def test_load_without_follow_through(self):
        r = self.rows(self.session(read(self.rich / "SKILL.md"), run("ls /tmp")))["rich"]
        self.assertEqual((r.sessions_loaded, r.sessions_followed), (1, 0))
        self.assertEqual(r.rate, 0.0)

    def test_access_before_load_or_rereading_skill_md_is_not_follow_through(self):
        r = self.rows(
            self.session(
                read(self.rich / "scripts" / "go.py"),
                read(self.rich / "SKILL.md"),
                read(self.rich / "SKILL.md"),
            )
        )["rich"]
        self.assertEqual(r.sessions_followed, 0)

    def test_files_of_another_skill_do_not_count(self):
        r = self.rows(self.session(read(self.rich / "SKILL.md"), read("/x/skills/richer/references/api.md")))["rich"]
        self.assertEqual(r.sessions_followed, 0)

    def test_repeated_loads_in_one_session_count_once(self):
        r = self.rows(self.session(read(self.rich / "SKILL.md"), read(self.rich / "SKILL.md")))["rich"]
        self.assertEqual(r.sessions_loaded, 1)

    def test_skill_md_only_skill_is_not_scored(self):
        r = self.rows(self.session(read(self.bare / "SKILL.md")))["bare"]
        self.assertFalse(r.has_support)
        self.assertIsNone(r.rate)

    def test_skill_tool_load_is_recognised(self):
        r = self.rows(
            self.session(ft.ToolCall(None, "Skill", {"skill": "rich"}), read(self.rich / "references" / "api.md"))
        )["rich"]
        self.assertEqual(r.sessions_followed, 1)

    def test_days_and_agent_filters(self):
        old = self.session(read(self.rich / "SKILL.md"), sid="old", ts=datetime(2026, 1, 1, tzinfo=timezone.utc))
        other = self.session(read(self.rich / "SKILL.md"), sid="c", agent="claude")
        self.assertNotIn("rich", self.rows(old, days=30))
        self.assertNotIn("rich", self.rows(other, agent="antigravity"))
        self.assertIn("rich", self.rows(other, agent="all"))

    def test_render_flags_low_rate_only_with_enough_sessions(self):
        sessions = [self.session(read(self.rich / "SKILL.md"), sid=str(i)) for i in range(5)]
        sessions.append(self.session(read(self.bare / "SKILL.md"), sid="b"))
        sessions.append(self.session(read("/elsewhere/skills/ghost/SKILL.md"), sid="g"))
        text = ft.render(ft.analyze(sessions, self.copies, NOW), min_sessions=5)
        low = text.split("## Low follow-through")[1].split("##")[0]
        self.assertIn("- rich: 0 of 5 sessions", low)
        self.assertIn("- bare: 1 sessions", text.split("SKILL.md only)")[1])
        self.assertIn("- ghost: 1 sessions", text.split("not in current inventory")[1])
        self.assertNotIn(str(self.home), text)
        quiet = ft.render(ft.analyze(sessions[:2], self.copies, NOW), min_sessions=5)
        self.assertIn("None.", quiet.split("## Low follow-through")[1].split("##")[0])

    def test_write_csv_statuses_and_columns(self):
        ok_skill = self.home / ".gemini" / "config" / "skills" / "ok-skill"
        touch(ok_skill / "SKILL.md")
        touch(ok_skill / "scripts" / "run.sh")
        few_skill = self.home / ".gemini" / "config" / "skills" / "few-skill"
        touch(few_skill / "SKILL.md")
        touch(few_skill / "references" / "doc.md")
        copies = inventory.scan(self.home, [], agent="antigravity")

        sessions = []
        # rich: 5 sessions, 1 followed -> rate 0.2 < 0.3 -> low-followthrough
        sessions.append(self.session(read(self.rich / "SKILL.md"), read(self.rich / "references" / "api.md"), sid="r0"))
        for i in range(1, 5):
            sessions.append(self.session(read(self.rich / "SKILL.md"), sid=f"r{i}"))
        # ok-skill: 5 sessions, 4 followed -> rate 0.8 >= 0.3 -> ok
        for i in range(4):
            sessions.append(self.session(read(ok_skill / "SKILL.md"), run(f"bash {ok_skill}/scripts/run.sh"), sid=f"ok{i}"))
        sessions.append(self.session(read(ok_skill / "SKILL.md"), sid="ok4"))
        # few-skill: 2 sessions (< 5), 0 followed -> insufficient-sessions
        for i in range(2):
            sessions.append(self.session(read(few_skill / "SKILL.md"), sid=f"f{i}"))
        # bare: 1 session, no support files -> no-support-files
        sessions.append(self.session(read(self.bare / "SKILL.md"), sid="b0"))
        # ghost: 1 session, not in inventory -> unmatched
        sessions.append(self.session(read("/elsewhere/skills/ghost/SKILL.md"), sid="g0"))

        analyzed = ft.analyze(sessions, copies, NOW)
        csv_path = self.home / "out.csv"
        ft.write_csv(analyzed, csv_path, min_sessions=5, low_rate=0.3)

        raw_csv = csv_path.read_text(encoding="utf-8")
        self.assertNotIn(str(self.home), raw_csv)
        with open(csv_path, encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            self.assertEqual(
                reader.fieldnames,
                [
                    "skill",
                    "sessions_loaded",
                    "sessions_scored",
                    "sessions_followed",
                    "rate",
                    "read_reference",
                    "ran_script",
                    "status",
                ],
            )
            by_skill = {row["skill"]: row for row in reader}

        self.assertEqual(by_skill["rich"]["status"], "low-followthrough")
        self.assertEqual(by_skill["rich"]["rate"], "0.2")
        self.assertEqual(by_skill["rich"]["read_reference"], "1")
        self.assertEqual(by_skill["rich"]["ran_script"], "0")

        self.assertEqual(by_skill["ok-skill"]["status"], "ok")
        self.assertEqual(by_skill["ok-skill"]["rate"], "0.8")
        self.assertEqual(by_skill["ok-skill"]["ran_script"], "4")

        self.assertEqual(by_skill["few-skill"]["status"], "insufficient-sessions")
        self.assertEqual(by_skill["few-skill"]["rate"], "0.0")

        self.assertEqual(by_skill["bare"]["status"], "no-support-files")
        self.assertEqual(by_skill["bare"]["rate"], "")

        self.assertEqual(by_skill["ghost"]["status"], "unmatched")
        self.assertEqual(by_skill["ghost"]["rate"], "")

    def test_reads_antigravity_and_claude_transcripts_including_slash_loads(self):
        agy = self.home / ".gemini" / "antigravity" / "brain" / "conv1" / ".system_generated" / "logs" / "transcript.jsonl"
        steps = [
            {"step_index": 0, "type": "USER_INPUT", "created_at": "2026-11-24T10:00:00Z", "content": "<USER_REQUEST>/rich go</USER_REQUEST>"},
            {"step_index": 1, "type": "PLANNER_RESPONSE", "created_at": "2026-11-24T10:00:02Z",
             "tool_calls": [{"name": "run_command", "args": json.dumps({"CommandLine": f"python {self.rich}/scripts/go.py"})}]},
        ]
        touch(agy, "\n".join(json.dumps(s) for s in steps) + "\n")

        claude_skill = self.home / ".claude" / "skills" / "claude-rich"
        touch(claude_skill / "SKILL.md")
        touch(claude_skill / "references" / "guide.md")
        claude = self.home / ".claude" / "projects" / "p" / "sess.jsonl"
        claude_lines = [
            {
                "sessionId": "c1",
                "timestamp": "2026-11-24T10:00:00Z",
                "message": {"role": "user", "content": "<command-name>claude-rich</command-name>"},
            },
            {
                "sessionId": "c1",
                "timestamp": "2026-11-24T10:00:05Z",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "name": "Read", "input": {"file_path": str(claude_skill / "references" / "guide.md")}}],
                },
            },
        ]
        touch(claude, "\n".join(json.dumps(e) for e in claude_lines) + "\n")

        all_copies = inventory.scan(self.home, [])
        sessions = list(ft.collect_sessions(self.home))
        self.assertEqual(sorted(s.agent for s in sessions), ["antigravity", "claude"])
        rows = {x.name: x for x in ft.analyze(sessions, all_copies, NOW, days=30)}
        self.assertEqual((rows["rich"].sessions_loaded, rows["rich"].sessions_followed, rows["rich"].kinds["script"]), (1, 1, 1))
        self.assertEqual((rows["claude-rich"].sessions_loaded, rows["claude-rich"].sessions_followed, rows["claude-rich"].kinds["reference"]), (1, 1, 1))

        out = io.StringIO()
        with patch.object(Path, "home", return_value=self.home), contextlib.redirect_stdout(out):
            code = skill_usage.main(["followthrough", "--agent", "antigravity", "--workspace", str(self.home)])
        self.assertEqual(code, 0)
        self.assertIn("| rich | 1 | 1 | 1 | 100% | 0 | 1 |", out.getvalue())

    def test_reads_pi_sessions_with_skill_colon_and_tool_calls(self):
        pi_skill = self.home / ".pi" / "agent" / "skills" / "pi-flow"
        touch(pi_skill / "SKILL.md")
        touch(pi_skill / "scripts" / "check.sh")
        touch(pi_skill / "references" / "spec.md")

        # Session 1: /skill:pi-flow followed by bash running check.sh
        pi_s1 = self.home / ".pi" / "agent" / "sessions" / "--proj--" / "s1.jsonl"
        s1_entries = [
            {"type": "session", "id": "pi-1", "timestamp": "2026-11-24T09:00:00Z", "cwd": "/tmp/proj"},
            {"type": "message", "timestamp": "2026-11-24T09:00:01Z", "message": {"role": "user", "content": "/skill:pi-flow run"}},
            {
                "type": "message",
                "timestamp": "2026-11-24T09:00:05Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "toolCall", "name": "bash", "arguments": {"command": f"bash {pi_skill}/scripts/check.sh"}}
                    ],
                },
            },
        ]
        touch(pi_s1, "\n".join(json.dumps(e) for e in s1_entries) + "\n")

        # Session 2: /pi-flow followed only by reading README.md (metadata -> should NOT count as follow-through)
        pi_s2 = self.home / ".pi" / "agent" / "sessions" / "--proj--" / "s2.jsonl"
        s2_entries = [
            {"type": "session", "id": "pi-2", "timestamp": "2026-11-24T09:10:00Z"},
            {
                "type": "message",
                "timestamp": "2026-11-24T09:10:01Z",
                "message": {"role": "user", "content": [{"type": "text", "text": "/pi-flow inspect"}]},
            },
            {
                "type": "message",
                "timestamp": "2026-11-24T09:10:03Z",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "toolCall", "name": "read", "arguments": json.dumps({"path": str(pi_skill / "README.md")})}],
                },
            },
        ]
        touch(pi_s2, "\n".join(json.dumps(e) for e in s2_entries) + "\n")

        copies = inventory.scan(self.home, [], agent="pi")
        sessions = list(ft.collect_sessions(self.home, agent="pi"))
        self.assertEqual(len(sessions), 2)
        r = {x.name: x for x in ft.analyze(sessions, copies, NOW, days=30, agent="pi")}["pi-flow"]
        self.assertEqual((r.sessions_loaded, r.sessions_scored, r.sessions_followed), (2, 2, 1))
        self.assertEqual(r.kinds, {"reference": 0, "script": 1})

    def test_reads_hermes_sqlite_sessions_and_skill_view_follow_through(self):
        hermes_skill = self.home / ".hermes" / "skills" / "github" / "pr-review"
        touch(hermes_skill / "SKILL.md")
        touch(hermes_skill / "references" / "checklist.md")
        touch(hermes_skill / "scripts" / "lint.py")

        db_path = self.home / ".hermes" / "state.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        ts_base = datetime(2026, 11, 24, 8, 0, tzinfo=timezone.utc).timestamp()
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("CREATE TABLE sessions (id TEXT, started_at REAL, cwd TEXT, source TEXT)")
            conn.execute(
                "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, tool_calls TEXT, timestamp REAL, active INTEGER)"
            )
            # Session h1: invoked via [IMPORTANT: ...] banner, followed by skill_view reference read
            conn.execute("INSERT INTO sessions VALUES ('h1', ?, '/tmp/p', 'cli')", (ts_base,))
            conn.execute(
                "INSERT INTO messages VALUES (1, 'h1', 'user', '[IMPORTANT: The user has invoked the \"/pr-review\" skill]', NULL, ?, 1)",
                (ts_base + 1,),
            )
            conn.execute(
                "INSERT INTO messages VALUES (2, 'h1', 'assistant', '', ?, ?, 1)",
                (
                    json.dumps([{"function": {"name": "skill_view", "arguments": json.dumps({"name": "pr-review", "file_path": "references/checklist.md"})}}]),
                    ts_base + 5,
                ),
            )
            # Inactive message in h1 should be ignored
            conn.execute(
                "INSERT INTO messages VALUES (3, 'h1', 'assistant', '', ?, ?, 0)",
                (
                    json.dumps([{"function": {"name": "skill_view", "arguments": json.dumps({"name": "pr-review", "file_path": "scripts/lint.py"})}}]),
                    ts_base + 6,
                ),
            )
            # Session h2: loaded via skill_view(name='pr-review'), followed by nested category path read
            conn.execute("INSERT INTO sessions VALUES ('h2', ?, '/tmp/p', 'cli')", (ts_base + 100,))
            conn.execute(
                "INSERT INTO messages VALUES (4, 'h2', 'assistant', '', ?, ?, 1)",
                (
                    json.dumps([
                        {"function": {"name": "skill_view", "arguments": json.dumps({"name": "pr-review"})}},
                        {"function": {"name": "terminal", "arguments": json.dumps({"command": f"python3 {hermes_skill}/scripts/lint.py"})}},
                    ]),
                    ts_base + 105,
                ),
            )
            conn.commit()
        finally:
            conn.close()

        copies = inventory.scan(self.home, [], agent="hermes")
        sessions = list(ft.collect_sessions(self.home, agent="hermes"))
        self.assertEqual(len(sessions), 2)
        r = {x.name: x for x in ft.analyze(sessions, copies, NOW, days=30, agent="hermes")}["pr-review"]
        self.assertEqual((r.sessions_loaded, r.sessions_scored, r.sessions_followed), (2, 2, 2))
        self.assertEqual(r.kinds, {"reference": 1, "script": 1})

    def test_reads_codex_rollout_jsonl_and_json_sessions_while_ignoring_history(self):
        codex_skill = self.home / ".codex" / "skills" / "codex-audit"
        touch(codex_skill / "SKILL.md")
        touch(codex_skill / "references" / "rules.md")
        touch(codex_skill / "scripts" / "scan.py")

        # Ignored history/index files must not create sessions
        touch(
            self.home / ".codex" / "history" / "history.jsonl",
            json.dumps({"timestamp": "2026-11-24T07:00:00Z", "role": "user", "content": "/codex-audit"}) + "\n",
        )
        touch(
            self.home / ".codex" / "session_index.jsonl",
            json.dumps({"id": "idx-1", "timestamp": "2026-11-24T07:00:00Z"}) + "\n",
        )

        # Rollout JSONL with Codex request header + response_item function_call
        rollout = self.home / ".codex" / "sessions" / "2026" / "11" / "24" / "rollout-1.jsonl"
        rollout_lines = [
            {"id": "msg-001", "type": "event_msg", "timestamp": "2026-11-24T08:00:00Z",
             "payload": {"type": "user_message", "message": "## My request for Codex:\n/codex-audit check repo"}},
            {"type": "session_meta", "timestamp": "2026-11-24T08:00:01Z",
             "payload": {"id": "codex-uuid-1", "cwd": "/tmp/repo"}},
            {"id": "call-001", "type": "response_item", "timestamp": "2026-11-24T08:00:05Z",
             "payload": {"type": "function_call", "name": "read_file",
                         "arguments": json.dumps({"filePath": str(codex_skill / "references" / "rules.md")})}},
            {"id": "call-002", "type": "response_item", "timestamp": "2026-11-24T08:00:08Z",
             "payload": {"type": "function_call", "name": "shell",
                         "arguments": {"command": ["bash", "-lc", f"python3 {codex_skill}/scripts/scan.py"]}}},
        ]
        touch(rollout, "\n".join(json.dumps(e) for e in rollout_lines) + "\n")

        # JSON session file with items wrapper where agent only reads agents/openai.yaml (metadata -> unfollowed)
        json_sess = self.home / ".codex" / "sessions" / "session_items.json"
        touch(
            json_sess,
            json.dumps({
                "id": "codex-uuid-2",
                "items": [
                    {"type": "user_message", "timestamp": "2026-11-24T09:00:00Z", "message": {"content": "/codex-audit"}},
                    {
                        "type": "response_item",
                        "timestamp": "2026-11-24T09:00:02Z",
                        "payload": {
                            "type": "function_call",
                            "name": "read_file",
                            "arguments": {"path": str(codex_skill / "agents" / "openai.yaml")},
                        },
                    },
                ],
            }),
        )

        copies = inventory.scan(self.home, [], agent="codex")
        sessions = list(ft.collect_sessions(self.home, agent="codex"))
        self.assertEqual({s.session_id for s in sessions}, {"codex-uuid-1", "codex-uuid-2"})
        r = {x.name: x for x in ft.analyze(sessions, copies, NOW, days=30, agent="codex")}["codex-audit"]
        self.assertEqual((r.sessions_loaded, r.sessions_scored, r.sessions_followed), (2, 2, 1))
        self.assertEqual(r.kinds, {"reference": 1, "script": 1})

    def test_support_is_judged_on_the_copy_that_was_loaded(self):
        # Same name, one copy with support files and one without.
        other = self.home / ".agents" / "skills" / "bare" / "SKILL.md"
        touch(other)
        touch(other.parent / "references" / "x.md")
        copies = inventory.scan(self.home, [])
        rows = {r.name: r for r in ft.analyze([self.session(read(self.bare / "SKILL.md"))], copies, NOW)}
        self.assertEqual((rows["bare"].sessions_loaded, rows["bare"].sessions_scored), (1, 0))
        rows = {r.name: r for r in ft.analyze([self.session(read(other))], copies, NOW)}
        self.assertEqual(rows["bare"].sessions_scored, 1)
        # Slash load followed by reading the bare copy refines support to the specific copy read
        rows = {
            r.name: r
            for r in ft.analyze([self.session(ft.prompt_call("/bare"), read(self.bare / "SKILL.md"))], copies, NOW)
        }
        self.assertEqual((rows["bare"].sessions_loaded, rows["bare"].sessions_scored), (1, 0))

    def test_files_linked_from_skill_md_count_as_follow_through(self):
        helper = self.home / ".agents" / "skills" / "helper" / "SKILL.md"
        touch(helper)
        touch(self.bare / "SKILL.md", f"Use [helper]({helper}) and [docs](https://example.com).")
        r = self.rows(self.session(read(self.bare / "SKILL.md")))["bare"]
        self.assertEqual(r.sessions_scored, 1)
        self.assertEqual(r.sessions_followed, 0)
        r = self.rows(self.session(read(self.bare / "SKILL.md"), read(helper)))["bare"]
        self.assertEqual((r.sessions_followed, r.kinds["reference"]), (1, 1))

    def test_linked_files_resolve_relative_and_file_urls_and_skip_missing(self):
        touch(self.home / "shared" / "fmt.md")
        touch(self.bare / "SKILL.md", "[a](../../../../shared/fmt.md) [b](file:///nope/missing.md) [c](#anchor)")
        self.assertEqual(ft.linked_files(str(self.bare / "SKILL.md")), frozenset({str(self.home / "shared" / "fmt.md")}))


if __name__ == "__main__":
    unittest.main()
