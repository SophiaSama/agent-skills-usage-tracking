import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import hook
import skill_usage
from tracker import dashboard, detect, inventory, report, store, sync

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
TS = "2026-10-15T10:00:00Z"


def touch(path: Path, text: str = "skill") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class MultiAgentTests(unittest.TestCase):
    def test_inventory_scans_antigravity_pi_hermes_and_filters_by_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            ws = root / "ws"

            # Shared (.agents/skills) -> all 4 agents
            touch(home / ".agents/skills/shared-skill/SKILL.md")
            # Antigravity
            touch(home / ".gemini/config/skills/agy-personal/SKILL.md")
            touch(home / ".gemini/config/plugins/chrome-devtools-plugin/skills/chrome-devtools/SKILL.md")
            touch(home / ".gemini/antigravity/builtin/skills/antigravity_guide/SKILL.md")
            touch(ws / ".agent/skills/agy-project/SKILL.md")
            # Pi Agent
            touch(home / ".pi/agent/skills/pi-personal/SKILL.md")
            touch(home / ".pi/agent/git/github.com/obra/superpowers/skills/brainstorming/SKILL.md")
            touch(ws / ".pi/skills/pi-project/SKILL.md")
            # Hermes Agent
            touch(home / ".hermes/skills/.bundled_manifest", "github-code-review: 123\n")
            touch(home / ".hermes/skills/github/github-code-review/SKILL.md")
            touch(home / ".hermes/skills/custom/hermes-custom/SKILL.md")
            touch(ws / ".hermes/skills/hermes-project/SKILL.md")

            all_copies = {c.name: c for c in inventory.scan(home, [ws])}
            self.assertEqual(all_copies["shared-skill"].agents, inventory.AGENTS)
            self.assertEqual(all_copies["agy-personal"].source, "personal")
            self.assertEqual(all_copies["chrome-devtools"].source, "plugin:antigravity/chrome-devtools-plugin")
            self.assertEqual(all_copies["antigravity_guide"].source, "builtin:antigravity")
            self.assertEqual(all_copies["brainstorming"].source, "plugin:pi/obra/superpowers")
            self.assertEqual(all_copies["github-code-review"].source, "builtin:hermes")
            self.assertEqual(all_copies["hermes-custom"].source, "personal")

            agy_names = inventory.known_names(home, [ws], agent="antigravity")
            self.assertEqual(
                agy_names,
                {"shared-skill", "agy-personal", "chrome-devtools", "antigravity_guide", "agy-project"},
            )
            pi_names = inventory.known_names(home, [ws], agent="pi")
            self.assertEqual(pi_names, {"shared-skill", "pi-personal", "brainstorming", "pi-project"})
            hermes_names = inventory.known_names(home, [ws], agent="hermes")
            self.assertEqual(
                hermes_names,
                {"shared-skill", "github-code-review", "hermes-custom", "hermes-project"},
            )

    def test_detect_handles_antigravity_pi_and_hermes_tool_shapes(self):
        # Antigravity transcript toolCall with inner JSON quotes
        agy_read = {
            "timestamp": TS,
            "session_id": "conv-1",
            "agent": "antigravity",
            "toolCall": {
                "name": "view_file",
                "args": {
                    "AbsolutePath": '"/Users/u/.gemini/antigravity/builtin/skills/antigravity_guide/SKILL.md"',
                },
            },
        }
        rec = detect.detect(agy_read, set)
        self.assertEqual(rec["skill"], "antigravity_guide")
        self.assertEqual(rec["source"], "agent-read")
        self.assertEqual(rec["agent"], "antigravity")

        # Paginated continuation read is ignored
        agy_page2 = {
            "timestamp": TS,
            "session_id": "conv-1",
            "agent": "antigravity",
            "toolCall": {
                "name": "view_file",
                "args": {
                    "AbsolutePath": '"/Users/u/.gemini/antigravity/builtin/skills/antigravity_guide/SKILL.md"',
                    "StartLine": "200",
                },
            },
        }
        self.assertIsNone(detect.detect(agy_page2, set))

        # Hermes skill_view tool call
        hermes_view = {
            "hook_event_name": "PostToolUse",
            "timestamp": TS,
            "session_id": "h-1",
            "agent": "hermes",
            "tool_name": "skill_view",
            "tool_input": {"name": "github-code-review"},
        }
        h_rec = detect.detect(hermes_view, set)
        self.assertEqual(h_rec["skill"], "github-code-review")
        self.assertEqual(h_rec["source"], "skill-tool")
        self.assertEqual(h_rec["agent"], "hermes")

        # Hermes skill_view for a secondary reference file is ignored
        hermes_ref = {
            **hermes_view,
            "tool_input": {"name": "github-code-review", "file_path": "references/guide.md"},
        }
        self.assertIsNone(detect.detect(hermes_ref, set))

    def test_hook_emits_antigravity_allow_response_only_for_antigravity(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            with patch.dict("os.environ", {"SKILL_USAGE_DATA": str(data_dir)}):
                # Antigravity PreToolUse -> {"decision": "allow"}
                out = io.StringIO()
                payload = {
                    "conversationId": "c1",
                    "workspacePaths": ["/work/proj"],
                    "toolCall": {
                        "name": "view_file",
                        "args": {"AbsolutePath": "/Users/u/.agents/skills/my-skill/SKILL.md"},
                    },
                }
                with patch("sys.stdin", io.StringIO(json.dumps(payload))), contextlib.redirect_stdout(out):
                    self.assertEqual(hook.main(), 0)
                self.assertEqual(json.loads(out.getvalue()), {"decision": "allow"})

                # Copilot PostToolUse -> empty stdout
                out_copilot = io.StringIO()
                cp_payload = {
                    "hook_event_name": "SessionStart",
                    "session_id": "s1",
                    "cwd": "/work/proj",
                }
                with patch("sys.stdin", io.StringIO(json.dumps(cp_payload))), contextlib.redirect_stdout(out_copilot):
                    self.assertEqual(hook.main(), 0)
                self.assertEqual(out_copilot.getvalue(), "")

    def test_sync_collects_from_antigravity_pi_and_hermes_without_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            data_dir = root / "data"
            touch(home / ".agents/skills/slash-skill/SKILL.md")

            # 1. Antigravity transcript
            agy_log = home / ".gemini/antigravity/brain/conv-123/.system_generated/logs/transcript.jsonl"
            agy_log.parent.mkdir(parents=True, exist_ok=True)
            agy_steps = [
                {
                    "type": "USER_INPUT",
                    "created_at": "2026-10-16T09:00:00Z",
                    "content": "<USER_REQUEST>/slash-skill run check</USER_REQUEST>",
                },
                {
                    "type": "PLANNER_RESPONSE",
                    "created_at": "2026-10-16T09:00:05Z",
                    "tool_calls": [
                        {
                            "name": "view_file",
                            "args": {
                                "AbsolutePath": '"/Users/u/.gemini/antigravity/builtin/skills/antigravity_guide/SKILL.md"'
                            },
                        }
                    ],
                },
            ]
            agy_log.write_text("\n".join(json.dumps(s) for s in agy_steps) + "\n", encoding="utf-8")

            # 2. Pi Agent session JSONL
            pi_log = home / ".pi/agent/sessions/--Users-u--/sess-1.jsonl"
            pi_log.parent.mkdir(parents=True, exist_ok=True)
            pi_entries = [
                {"type": "session", "id": "pi-sess-1", "timestamp": "2026-10-16T10:00:00Z", "cwd": "/Users/u/proj"},
                {
                    "type": "message",
                    "timestamp": "2026-10-16T10:01:00Z",
                    "message": {"role": "user", "content": "/skill:brainstorming design this"},
                },
                {
                    "type": "message",
                    "timestamp": "2026-10-16T10:02:00Z",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "toolCall",
                                "name": "read",
                                "arguments": {
                                    "path": "/Users/u/.pi/agent/git/github.com/obra/superpowers/skills/systematic-debugging/SKILL.md"
                                },
                            }
                        ],
                    },
                },
            ]
            pi_log.write_text("\n".join(json.dumps(e) for e in pi_entries) + "\n", encoding="utf-8")

            # 3. Hermes state.db + .usage.json
            hermes_dir = home / ".hermes"
            hermes_dir.mkdir(parents=True, exist_ok=True)
            db_path = hermes_dir / "state.db"
            conn = sqlite3.connect(str(db_path))
            conn.execute("CREATE TABLE sessions (id TEXT, started_at REAL, cwd TEXT, source TEXT)")
            conn.execute(
                "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, tool_calls TEXT, timestamp REAL, active INTEGER)"
            )
            conn.execute("INSERT INTO sessions VALUES ('h-sess-1', 1792140000.0, '/Users/u/proj', 'cli')")
            conn.execute(
                "INSERT INTO messages VALUES (1, 'h-sess-1', 'user', '[IMPORTANT: The user has invoked the \"/hermes-slash\" skill]', NULL, 1792140010.0, 1)"
            )
            conn.execute(
                "INSERT INTO messages VALUES (2, 'h-sess-1', 'assistant', '', ?, 1792140020.0, 1)",
                (json.dumps([{"function": {"name": "skill_view", "arguments": json.dumps({"name": "find-skills"})}}]),),
            )
            conn.commit()
            conn.close()

            touch(
                hermes_dir / "skills/.usage.json",
                json.dumps({"fallback-skill": {"use_count": 2, "last_used_at": 1792140050.0}}),
            )

            first_counts = sync.sync_events(data_dir=data_dir, home=home, workspaces=[])
            self.assertEqual(first_counts["antigravity"], 3)
            self.assertEqual(first_counts["pi"], 3)
            self.assertEqual(first_counts["hermes"], 4)
            self.assertEqual(first_counts["total"], 10)

            # Second sync should be idempotent (0 new events)
            second_counts = sync.sync_events(data_dir=data_dir, home=home, workspaces=[])
            self.assertEqual(second_counts["total"], 0)

            events = store.read_events(data_dir)
            agy_summary = report.summarize(events, [], NOW, 30, agent="antigravity")
            self.assertEqual({r.name for r in agy_summary.rows}, {"slash-skill", "antigravity_guide"})

            pi_summary = report.summarize(events, [], NOW, 30, agent="pi")
            self.assertEqual({r.name for r in pi_summary.rows}, {"brainstorming", "systematic-debugging"})

            hermes_summary = report.summarize(events, [], NOW, 30, agent="hermes")
            self.assertEqual({r.name for r in hermes_summary.rows}, {"hermes-slash", "find-skills", "fallback-skill"})

    def test_install_hooks_and_cli_subcommands(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            data_dir = root / "data"
            with patch("skill_usage.Path.home", return_value=home):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    code = skill_usage.main(["install-hooks"])
                self.assertEqual(code, 0)
                self.assertTrue((home / ".gemini/config/hooks.json").is_file())
                self.assertTrue((home / ".pi/agent/extensions/skill-usage.ts").is_file())
                self.assertTrue((home / ".hermes/plugins/skill-usage/__init__.py").is_file())

                sync_buf = io.StringIO()
                with contextlib.redirect_stdout(sync_buf):
                    code = skill_usage.main(["sync", "--data-dir", str(data_dir)])
                self.assertEqual(code, 0)
                self.assertIn("Synced 0 new events", sync_buf.getvalue())

    def test_dashboard_includes_per_agent_views_and_controls(self):
        copies = [
            inventory.SkillCopy("agy-skill", "/p/agy/SKILL.md", "builtin:antigravity", ("antigravity",)),
            inventory.SkillCopy("pi-skill", "/p/pi/SKILL.md", "personal", ("pi",)),
        ]
        events = [
            {
                "type": "load",
                "ts": (NOW - timedelta(days=1)).isoformat(),
                "session_id": "s1",
                "agent": "antigravity",
                "skill": "agy-skill",
                "source": "agent-read",
            }
        ]
        data = dashboard.build_dashboard_data(events, copies, NOW)
        self.assertEqual(data["agents"]["antigravity"]["30d"]["summary"]["installedSkills"], 1)
        self.assertEqual(data["agents"]["antigravity"]["30d"]["summary"]["recordedLoads"], 1)
        self.assertEqual(data["agents"]["pi"]["30d"]["summary"]["installedSkills"], 1)
        self.assertEqual(data["agents"]["pi"]["30d"]["summary"]["recordedLoads"], 0)

        html = dashboard.render_html(data)
        self.assertIn('id="agent-control"', html)
        self.assertIn('data-agent="antigravity"', html)
        self.assertIn('data-agent="pi"', html)
        self.assertIn('data-agent="hermes"', html)
        self.assertIn('data-filter="non-builtin"', html)

    def test_live_hook_followed_by_sync_deduplicates_without_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            data_dir = root / "data"
            skill_dir = home / ".gemini" / "config" / "plugins" / "my-plugin" / "skills" / "my-skill"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("# My Skill\n", encoding="utf-8")

            # 1. Live hook records session start and view_file
            hook.run({
                "conversationId": "conv-100",
                "invocationNum": 1,
                "workspacePaths": ["/Users/test/myproject"],
                "timestamp": "2026-11-08T04:00:00Z",
            }, data_dir=data_dir, home=home)

            hook.run({
                "conversationId": "conv-100",
                "invocationNum": 2,
                "workspacePaths": ["/Users/test/myproject"],
                "timestamp": "2026-11-08T04:00:06Z",
                "toolCall": {
                    "name": "view_file",
                    "args": {"AbsolutePath": str(skill_dir / "SKILL.md")},
                },
            }, data_dir=data_dir, home=home)

            self.assertEqual(len(store.read_events(data_dir)), 2)

            # 2. Antigravity transcript contains prompt and tool call
            agy_log = home / ".gemini/antigravity/brain/conv-100/.system_generated/logs/transcript.jsonl"
            agy_log.parent.mkdir(parents=True, exist_ok=True)
            steps = [
                {
                    "type": "USER_INPUT",
                    "created_at": "2026-11-08T04:00:01Z",
                    "content": "<USER_REQUEST>/my-skill do work</USER_REQUEST>",
                },
                {
                    "type": "PLANNER_RESPONSE",
                    "created_at": "2026-11-08T04:00:06Z",
                    "tool_calls": [
                        {
                            "name": "view_file",
                            "args": {"AbsolutePath": str(skill_dir / "SKILL.md"), "Cwd": "/Users/test/myproject"},
                        }
                    ],
                },
            ]
            agy_log.write_text("\n".join(json.dumps(s) for s in steps) + "\n", encoding="utf-8")

            # 3. Sync runs: session and view_file are deduplicated; user-slash is added
            counts = sync.sync_events(data_dir=data_dir, home=home, workspaces=[])
            self.assertEqual(counts["antigravity"], 1)
            self.assertEqual(counts["total"], 1)

            events = store.read_events(data_dir)
            self.assertEqual(len(events), 3)
            types_sources = [(e.get("type"), e.get("source", "")) for e in events]
            self.assertIn(("session", ""), types_sources)
            self.assertIn(("load", "agent-read"), types_sources)
            self.assertIn(("load", "user-slash"), types_sources)

    def test_live_hook_followed_by_sync_deduplicates_with_latency_jitter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            data_dir = root / "data"
            skill_dir = home / ".gemini" / "config" / "plugins" / "my-plugin" / "skills" / "my-skill"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("# My Skill\n", encoding="utf-8")

            # 1. Live hook records session start at T0 and view_file at T0+2s
            hook.run({
                "conversationId": "conv-200",
                "invocationNum": 1,
                "workspacePaths": ["/Users/test/myproject"],
                "timestamp": "2026-11-08T05:00:00Z",
            }, data_dir=data_dir, home=home)

            hook.run({
                "conversationId": "conv-200",
                "invocationNum": 2,
                "workspacePaths": ["/Users/test/myproject"],
                "timestamp": "2026-11-08T05:00:02Z",
                "toolCall": {
                    "name": "view_file",
                    "args": {"AbsolutePath": str(skill_dir / "SKILL.md")},
                },
            }, data_dir=data_dir, home=home)

            # 2. Antigravity transcript logs step 14 seconds later (T0+16s, exceeding former 5s slack)
            agy_log = home / ".gemini/antigravity/brain/conv-200/.system_generated/logs/transcript.jsonl"
            agy_log.parent.mkdir(parents=True, exist_ok=True)
            steps = [
                {
                    "type": "USER_INPUT",
                    "created_at": "2026-11-08T05:00:01Z",
                    "content": "<USER_REQUEST>/my-skill run</USER_REQUEST>",
                },
                {
                    "type": "PLANNER_RESPONSE",
                    "created_at": "2026-11-08T05:00:16Z",
                    "tool_calls": [
                        {
                            "name": "view_file",
                            "args": {"AbsolutePath": str(skill_dir / "SKILL.md"), "Cwd": "/Users/test/myproject"},
                        }
                    ],
                },
            ]
            agy_log.write_text("\n".join(json.dumps(s) for s in steps) + "\n", encoding="utf-8")

            # 3. Sync runs: view_file at T0+16s is within 30s slack of T0+2s, so no duplicate read is added
            counts = sync.sync_events(data_dir=data_dir, home=home, workspaces=[])
            self.assertEqual(counts["antigravity"], 1)  # only the user-slash prompt event is newly added
            self.assertEqual(counts["total"], 1)

            events = store.read_events(data_dir)
            loads = [e for e in events if e.get("type") == "load" and e.get("source") == "agent-read"]
            self.assertEqual(len(loads), 1)

    def test_sync_collects_from_claude_transcripts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            data_dir = root / "data"
            skill_dir = home / ".claude" / "skills" / "tdd"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("# TDD\n", encoding="utf-8")

            proj_dir = home / ".claude" / "projects" / "test-proj"
            proj_dir.mkdir(parents=True)
            claude_log = proj_dir / "sess-c1.jsonl"
            lines = [
                {
                    "sessionId": "sess-c1",
                    "cwd": "/Users/u/test-proj",
                    "timestamp": "2026-11-08T06:00:00Z",
                    "message": {
                        "role": "user",
                        "content": "<command-name>tdd</command-name><command-args>run</command-args>",
                    },
                },
                {
                    "sessionId": "sess-c1",
                    "cwd": "/Users/u/test-proj",
                    "timestamp": "2026-11-08T06:00:05Z",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Read",
                                "input": {
                                    "file_path": str(skill_dir / "SKILL.md"),
                                    "offset": 1,
                                    "limit": 100,
                                },
                            }
                        ],
                    },
                },
                {
                    "sessionId": "sess-c1",
                    "cwd": "/Users/u/test-proj",
                    "timestamp": "2026-11-08T06:00:10Z",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Skill",
                                "input": {"skill": "tdd"},
                            }
                        ],
                    },
                },
            ]
            claude_log.write_text("\n".join(json.dumps(l) for l in lines) + "\n", encoding="utf-8")

            counts = sync.sync_events(data_dir=data_dir, home=home, workspaces=[], agents=["claude"])
            self.assertEqual(counts["claude"], 4)  # 1 session + 3 loads (slash, read, skill-tool)
            events = store.read_events(data_dir)
            claude_events = [e for e in events if e.get("agent") == "claude"]
            self.assertEqual(len(claude_events), 4)

            sources = [e.get("source") for e in claude_events if e.get("type") == "load"]
            self.assertEqual(sources, ["user-slash", "agent-read", "skill-tool"])

    def test_antigravity_sync_handles_numeric_epoch_timestamps(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            data_dir = root / "data"
            skill_dir = home / ".gemini" / "config" / "skills" / "custom-agy"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("# Skill\n", encoding="utf-8")

            agy_log = home / ".gemini" / "antigravity" / "brain" / "conv-epoch" / ".system_generated" / "logs" / "transcript.jsonl"
            agy_log.parent.mkdir(parents=True, exist_ok=True)
            steps = [
                {
                    "type": "USER_INPUT",
                    "created_at": 1792140000.0,  # Numeric epoch timestamp
                    "content": "<USER_REQUEST>/custom-agy test</USER_REQUEST>",
                },
                {
                    "type": "PLANNER_RESPONSE",
                    "created_at": 1792140005.0,
                    "tool_calls": [
                        {
                            "name": "view_file",
                            "args": {"AbsolutePath": str(skill_dir / "SKILL.md"), "cwd": "/Users/u/proj"},
                        }
                    ],
                },
            ]
            agy_log.write_text("\n".join(json.dumps(s) for s in steps) + "\n", encoding="utf-8")

            counts = sync.sync_events(data_dir=data_dir, home=home, workspaces=[], agents=["antigravity"])
            self.assertEqual(counts["antigravity"], 3)  # session + slash + read
            events = store.read_events(data_dir)
            self.assertTrue(all(isinstance(e.get("ts"), str) and "T" in e["ts"] for e in events))

    def test_sync_deduplicates_with_casing_differences(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            data_dir = root / "data"
            skill_dir = home / ".gemini" / "config" / "skills" / "TDD"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("# TDD\n", encoding="utf-8")

            # 1. Live hook records with skill="TDD" (uppercase)
            hook.run({
                "conversationId": "conv-case",
                "invocationNum": 1,
                "timestamp": "2026-11-08T07:00:00Z",
                "toolCall": {
                    "name": "view_file",
                    "args": {"AbsolutePath": str(skill_dir / "SKILL.md")},
                },
            }, data_dir=data_dir, home=home)
            self.assertEqual(len(store.read_events(data_dir)), 1)

            # 2. Transcript records same read with skill path in lowercase
            agy_log = home / ".gemini" / "antigravity" / "brain" / "conv-case" / ".system_generated" / "logs" / "transcript.jsonl"
            agy_log.parent.mkdir(parents=True, exist_ok=True)
            step = {
                "type": "PLANNER_RESPONSE",
                "created_at": "2026-11-08T07:00:05Z",
                "tool_calls": [
                    {
                        "name": "view_file",
                        "args": {"AbsolutePath": str(skill_dir / "SKILL.md")},
                    }
                ],
            }
            agy_log.write_text(json.dumps(step) + "\n", encoding="utf-8")

            # 3. Sync runs: read is deduplicated despite casing
            counts = sync.sync_events(data_dir=data_dir, home=home, workspaces=[], agents=["antigravity"])
            # session event may be added if first_ts, but agent-read load must not be duplicated
            events = store.read_events(data_dir)
            load_events = [e for e in events if e.get("type") == "load" and e.get("source") == "agent-read"]
            self.assertEqual(len(load_events), 1)

    def test_hermes_sync_schema_fallback_and_safe_usage_parsing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            data_dir = root / "data"
            hermes_dir = home / ".hermes"
            hermes_dir.mkdir(parents=True)

            # Messages table without 'active' column and without 'id' column
            db_path = hermes_dir / "state.db"
            conn = sqlite3.connect(str(db_path))
            conn.execute("CREATE TABLE sessions (id TEXT, started_at REAL, cwd TEXT, source TEXT)")
            conn.execute("CREATE TABLE messages (session_id TEXT, role TEXT, content TEXT, tool_calls TEXT, timestamp REAL)")
            conn.execute("INSERT INTO sessions VALUES ('h-1', 1792140000.0, '/Users/u/proj', 'cli')")
            conn.execute("INSERT INTO messages VALUES ('h-1', 'user', '/my-skill test', NULL, 1792140001.0)")
            conn.commit()
            conn.close()

            # .usage.json with invalid non-numeric use_count alongside valid entry
            usage_path = hermes_dir / "skills" / ".usage.json"
            usage_path.parent.mkdir(parents=True, exist_ok=True)
            usage_path.write_text(
                json.dumps({
                    "broken-skill": {"use_count": "invalid", "last_used_at": 1792140010.0},
                    "valid-skill": {"use_count": 5, "last_used_at": 1792140020.0},
                }),
                encoding="utf-8",
            )

            counts = sync.sync_events(data_dir=data_dir, home=home, workspaces=[], agents=["hermes"])
            self.assertGreaterEqual(counts["hermes"], 2)  # session + valid-skill (broken safely skipped)
            events = store.read_events(data_dir)
            skills = {e.get("skill") for e in events if e.get("type") == "load"}
            self.assertIn("valid-skill", skills)

    def test_antigravity_sync_tag_variations_and_fallback_transcript_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            data_dir = root / "data"
            touch(home / ".agents/skills/tag-skill/SKILL.md")
            touch(home / ".agents/skills/fallback-skill/SKILL.md")

            # 1. Conv 1: lowercase <user_request> and AGENT_RESPONSE with toolCalls
            conv1 = home / ".gemini/antigravity/brain/conv-tags/.system_generated/logs"
            conv1.mkdir(parents=True)
            log1 = conv1 / "transcript.jsonl"
            log1.write_text(
                json.dumps({
                    "type": "USER_INPUT",
                    "created_at": "2026-10-16T12:00:00Z",
                    "content": "<user_request>\n/tag-skill run test\n</user_request>",
                }) + "\n" +
                json.dumps({
                    "type": "AGENT_RESPONSE",
                    "created_at": "2026-10-16T12:00:05Z",
                    "toolCalls": [
                        {
                            "name": "view_file",
                            "args": json.dumps({"AbsolutePath": "/Users/u/.agents/skills/tag-skill/SKILL.md"}),
                        }
                    ],
                }) + "\n",
                encoding="utf-8",
            )

            # 2. Conv 2: direct transcript.jsonl under conv folder
            conv2 = home / ".gemini/antigravity-ide/brain/conv-direct"
            conv2.mkdir(parents=True)
            log2 = conv2 / "transcript.jsonl"
            log2.write_text(
                json.dumps({
                    "type": "USER_INPUT",
                    "created_at": "2026-10-16T13:00:00Z",
                    "content": "<USER_PROMPT>/fallback-skill check</USER_PROMPT>",
                }) + "\n",
                encoding="utf-8",
            )

            counts = sync.sync_events(data_dir=data_dir, home=home, workspaces=[], agents=["antigravity"])
            self.assertGreaterEqual(counts["antigravity"], 3)
            events = store.read_events(data_dir)
            skills = {e.get("skill") for e in events if e.get("type") == "load"}
            self.assertIn("tag-skill", skills)
            self.assertIn("fallback-skill", skills)

    def test_claude_and_pi_sync_with_string_tool_arguments_and_mixed_case_tags(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            data_dir = root / "data"
            touch(home / ".agents/skills/c-skill/SKILL.md")
            touch(home / ".agents/skills/pi-skill/SKILL.md")

            # Claude session with <Command-Name> and string tool arguments
            claude_proj = home / ".claude/projects/myproj"
            claude_proj.mkdir(parents=True)
            claude_log = claude_proj / "sess1.jsonl"
            claude_log.write_text(
                json.dumps({
                    "sessionId": "claude-s1",
                    "timestamp": "2026-10-16T14:00:00Z",
                    "message": {"role": "user", "content": "<Command-Name>c-skill</Command-Name>"},
                }) + "\n" +
                json.dumps({
                    "sessionId": "claude-s1",
                    "timestamp": "2026-10-16T14:00:05Z",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "View",
                                "arguments": json.dumps({"filePath": "/Users/u/.agents/skills/c-skill/SKILL.md"}),
                            }
                        ],
                    },
                }) + "\n",
                encoding="utf-8",
            )

            # Pi session with string tool arguments
            pi_sess = home / ".pi/agent/sessions/proj"
            pi_sess.mkdir(parents=True)
            pi_log = pi_sess / "sess1.jsonl"
            pi_log.write_text(
                json.dumps({
                    "type": "session",
                    "id": "pi-s1",
                    "timestamp": "2026-10-16T15:00:00Z",
                    "cwd": "/Users/u/proj",
                }) + "\n" +
                json.dumps({
                    "type": "message",
                    "timestamp": "2026-10-16T15:00:05Z",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "toolCall",
                                "name": "read",
                                "arguments": json.dumps({"path": "/Users/u/.agents/skills/pi-skill/SKILL.md"}),
                            }
                        ],
                    },
                }) + "\n",
                encoding="utf-8",
            )

            counts = sync.sync_events(data_dir=data_dir, home=home, workspaces=[], agents=["claude", "pi"])
            self.assertGreaterEqual(counts["claude"], 2)
            self.assertGreaterEqual(counts["pi"], 2)
            events = store.read_events(data_dir)
            skills = {e.get("skill") for e in events if e.get("type") == "load"}
            self.assertIn("c-skill", skills)
            self.assertIn("pi-skill", skills)


if __name__ == "__main__":
    unittest.main()

