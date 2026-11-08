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
                self.assertTrue((home / ".codex/hooks.json").is_file())

                sync_buf = io.StringIO()
                with contextlib.redirect_stdout(sync_buf):
                    code = skill_usage.main(["sync", "--data-dir", str(data_dir)])
                self.assertEqual(code, 0)
                self.assertIn("Synced 0 new events", sync_buf.getvalue())

    def test_dashboard_includes_per_agent_views_and_controls(self):
        copies = [
            inventory.SkillCopy("agy-skill", "/p/agy/SKILL.md", "builtin:antigravity", ("antigravity",)),
            inventory.SkillCopy("pi-skill", "/p/pi/SKILL.md", "personal", ("pi",)),
            inventory.SkillCopy("codex-skill", "/p/codex/SKILL.md", "personal", ("codex",)),
        ]
        events = [
            {
                "type": "load",
                "ts": (NOW - timedelta(days=1)).isoformat(),
                "session_id": "s1",
                "agent": "antigravity",
                "skill": "agy-skill",
                "source": "agent-read",
            },
            {
                "type": "load",
                "ts": (NOW - timedelta(days=2)).isoformat(),
                "session_id": "s2",
                "agent": "codex",
                "skill": "codex-skill",
                "source": "user-slash",
            },
        ]
        data = dashboard.build_dashboard_data(events, copies, NOW)
        self.assertEqual(data["agents"]["antigravity"]["30d"]["summary"]["installedSkills"], 1)
        self.assertEqual(data["agents"]["antigravity"]["30d"]["summary"]["recordedLoads"], 1)
        self.assertEqual(data["agents"]["pi"]["30d"]["summary"]["installedSkills"], 1)
        self.assertEqual(data["agents"]["pi"]["30d"]["summary"]["recordedLoads"], 0)
        self.assertEqual(data["agents"]["codex"]["30d"]["summary"]["installedSkills"], 1)
        self.assertEqual(data["agents"]["codex"]["30d"]["summary"]["recordedLoads"], 1)

        html = dashboard.render_html(data)
        self.assertIn('id="agent-control"', html)
        self.assertIn('data-agent="antigravity"', html)
        self.assertIn('data-agent="pi"', html)
        self.assertIn('data-agent="hermes"', html)
        self.assertIn('data-agent="codex"', html)
        self.assertIn('data-filter="non-builtin"', html)

    def test_inventory_scans_codex_skills_and_filters_by_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            ws = root / "ws"

            # Shared (.agents/skills) -> all agents including codex
            touch(home / ".agents/skills/shared-skill/SKILL.md")
            # Codex personal
            touch(home / ".codex/skills/codex-personal/SKILL.md")
            # Codex project
            touch(ws / ".codex/skills/codex-project/SKILL.md")
            # Copilot personal (not codex)
            touch(home / ".copilot/skills/copilot-only/SKILL.md")

            all_copies = {c.name: c for c in inventory.scan(home, [ws])}
            self.assertIn("codex", all_copies["shared-skill"].agents)
            self.assertEqual(all_copies["codex-personal"].source, "personal")
            self.assertEqual(all_copies["codex-personal"].agents, ("codex",))
            self.assertEqual(all_copies["codex-project"].source, "project")
            self.assertEqual(all_copies["codex-project"].agents, ("codex",))

            codex_names = inventory.known_names(home, [ws], agent="codex")
            self.assertEqual(
                codex_names,
                {"shared-skill", "codex-personal", "codex-project"},
            )
            codex_copies = inventory.scan(home, [ws], agent="codex")
            self.assertEqual(len(codex_copies), 3)
            self.assertEqual(
                {c.name for c in codex_copies},
                {"shared-skill", "codex-personal", "codex-project"},
            )

    def test_detect_handles_codex_tool_and_prompt_shapes(self):
        # 1. PostToolUse read_file with agent="codex"
        codex_read = {
            "hook_event_name": "PostToolUse",
            "timestamp": TS,
            "session_id": "codex-sess-1",
            "agent": "codex",
            "cwd": "/path/to/repo",
            "tool_name": "read_file",
            "tool_input": {
                "filePath": "/Users/u/.codex/skills/code-review/SKILL.md",
            },
        }
        rec = detect.detect(codex_read, set)
        self.assertEqual(rec["skill"], "code-review")
        self.assertEqual(rec["source"], "agent-read")
        self.assertEqual(rec["agent"], "codex")
        self.assertEqual(rec["workspace"], "repo")

        # 2. UserPromptSubmit with agent="codex"
        codex_slash = {
            "hook_event_name": "UserPromptSubmit",
            "timestamp": TS,
            "session_id": "codex-sess-1",
            "agent": "codex",
            "cwd": "/path/to/repo",
            "prompt": "/code-review inspect this diff",
        }
        rec_slash = detect.detect(codex_slash, lambda: {"code-review"})
        self.assertEqual(rec_slash["skill"], "code-review")
        self.assertEqual(rec_slash["source"], "user-slash")
        self.assertEqual(rec_slash["agent"], "codex")

        # 3. Inferred agent from transcript_path containing .codex
        codex_inferred = {
            "hook_event_name": "PostToolUse",
            "timestamp": TS,
            "session_id": "codex-sess-2",
            "transcript_path": "/Users/u/.codex/sessions/rollout-123.jsonl",
            "tool_name": "read",
            "tool_input": {
                "path": "/Users/u/.codex/skills/code-review/SKILL.md",
            },
        }
        rec_inferred = detect.detect(codex_inferred, set)
        self.assertEqual(rec_inferred["agent"], "codex")
        self.assertEqual(rec_inferred["source"], "agent-read")

        # 4. Skill tool invocation with agent="codex"
        codex_skill_tool = {
            "hook_event_name": "PostToolUse",
            "timestamp": TS,
            "session_id": "codex-sess-1",
            "agent": "codex",
            "tool_name": "Skill",
            "tool_input": {"skill": "code-review"},
        }
        rec_tool = detect.detect(codex_skill_tool, set)
        self.assertEqual(rec_tool["agent"], "codex")
        self.assertEqual(rec_tool["source"], "skill-tool")
        self.assertEqual(rec_tool["skill"], "code-review")

        # 5. Prompt wrapped in Codex header markdown
        codex_wrapped = {
            "hook_event_name": "UserPromptSubmit",
            "timestamp": TS,
            "session_id": "codex-sess-1",
            "agent": "codex",
            "prompt": "## My request for Codex:\n/code-review run check",
        }
        rec_wrapped = detect.detect(codex_wrapped, lambda: {"code-review"})
        self.assertEqual(rec_wrapped["skill"], "code-review")
        self.assertEqual(rec_wrapped["source"], "user-slash")

        # 6. Namespaced tool name and target_file path key
        codex_namespaced = {
            "hook_event_name": "PostToolUse",
            "timestamp": TS,
            "session_id": "codex-sess-1",
            "agent": "codex",
            "tool_name": "openai:read_file",
            "tool_input": {"target_file": "/Users/u/.codex/skills/code-review/SKILL.md"},
        }
        rec_ns = detect.detect(codex_namespaced, set)
        self.assertEqual(rec_ns["skill"], "code-review")
        self.assertEqual(rec_ns["source"], "agent-read")

        # 7. Paginated continuation read with startLine is ignored
        codex_page = {
            "hook_event_name": "PostToolUse",
            "timestamp": TS,
            "session_id": "codex-sess-1",
            "agent": "codex",
            "tool_name": "read_file",
            "tool_input": {
                "filePath": "/Users/u/.codex/skills/code-review/SKILL.md",
                "startLine": 100,
            },
        }
        self.assertIsNone(detect.detect(codex_page, set))

    def test_sync_collects_codex_rollout_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            data_dir = root / "data"
            touch(home / ".codex/skills/codex-slash/SKILL.md")
            touch(home / ".codex/skills/codex-tool/SKILL.md")

            # 1. Primary Codex rollout format (~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl)
            codex_log1 = home / ".codex/sessions/2026/10/16/rollout-2026-10-16-sess1.jsonl"
            codex_log1.parent.mkdir(parents=True, exist_ok=True)
            log1_entries = [
                {
                    "type": "session_meta",
                    "timestamp": "2026-10-16T10:00:00Z",
                    "payload": {
                        "id": "codex-s1",
                        "cwd": "/Users/u/codex-proj",
                        "timestamp": "2026-10-16T10:00:00Z",
                    },
                },
                {
                    "id": "msg-001",
                    "type": "event_msg",
                    "timestamp": "2026-10-16T10:01:00Z",
                    "payload": {
                        "type": "user_message",
                        "message": "/codex-slash check code",
                    },
                },
                {
                    "id": "call-001",
                    "type": "response_item",
                    "timestamp": "2026-10-16T10:01:05Z",
                    "payload": {
                        "id": "fn-call-001",
                        "type": "function_call",
                        "name": "read_file",
                        "arguments": json.dumps({"filePath": "/Users/u/.codex/skills/codex-slash/SKILL.md"}),
                    },
                },
                {
                    "id": "call-002",
                    "type": "response_item",
                    "timestamp": "2026-10-16T10:02:00Z",
                    "payload": {
                        "type": "function_call",
                        "name": "skill",
                        "arguments": json.dumps({"name": "codex-tool"}),
                    },
                },
            ]
            codex_log1.write_text("\n".join(json.dumps(e) for e in log1_entries) + "\n", encoding="utf-8")

            # 2. Second session with direct user message and tool_calls list
            codex_log2 = home / ".codex/sessions/2026/10/16/rollout-2026-10-16-sess2.jsonl"
            log2_entries = [
                {
                    "type": "session_meta",
                    "timestamp": "2026-10-16T11:00:00Z",
                    "payload": {
                        "id": "codex-s2",
                        "cwd": "/Users/u/codex-proj2",
                        "timestamp": "2026-10-16T11:00:00Z",
                    },
                },
                {
                    "id": "msg-002",
                    "type": "event_msg",
                    "timestamp": "2026-10-16T11:01:00Z",
                    "payload": {
                        "type": "user_message",
                        "message": "## My request for Codex:\n/codex-tool run",
                    },
                },
                {
                    "id": "call-003",
                    "type": "response_item",
                    "timestamp": "2026-10-16T11:01:10Z",
                    "payload": {
                        "id": "fn-call-002",
                        "type": "function_call",
                        "name": "view_file",
                        "arguments": {"AbsolutePath": "/Users/u/.codex/skills/codex-tool/SKILL.md"},
                    },
                },
            ]
            codex_log2.write_text("\n".join(json.dumps(e) for e in log2_entries) + "\n", encoding="utf-8")

            first_counts = sync.sync_events(data_dir=data_dir, home=home, workspaces=[])
            self.assertEqual(first_counts["codex"], 7)  # 2 sessions + 5 loads
            self.assertEqual(first_counts["total"], 7)

            # Idempotence: second sync adds 0
            second_counts = sync.sync_events(data_dir=data_dir, home=home, workspaces=[])
            self.assertEqual(second_counts["total"], 0)

            events = store.read_events(data_dir)
            # Item/call IDs must NOT clobber the session_id
            self.assertTrue(all(e.get("session_id") in ("codex-s1", "codex-s2") for e in events))
            self.assertFalse(any(e.get("session_id") in ("msg-001", "call-001", "call-002", "msg-002", "call-003") for e in events))
            codex_summary = report.summarize(events, [], NOW, 30, agent="codex")
            self.assertEqual({r.name for r in codex_summary.rows}, {"codex-slash", "codex-tool"})
            # In session 1, user-slash absorbs the subsequent agent-read within 120s
            self.assertGreaterEqual(codex_summary.merged, 1)

    def test_codex_live_hook_recording(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            data_dir = root / "data"
            skill_md = touch(home / ".codex/skills/live-skill/SKILL.md")

            # 1. Session start via hook.run
            start_payload = {
                "hook_event_name": "SessionStart",
                "timestamp": TS,
                "session_id": "codex-live-1",
                "cwd": "/Users/u/my-app",
                "agent": "codex",
            }
            hook.run(start_payload, data_dir=data_dir, home=home)

            # 2. Tool use via hook.run with agent CLI parameter
            tool_payload = {
                "hook_event_name": "PostToolUse",
                "timestamp": TS,
                "session_id": "codex-live-1",
                "cwd": "/Users/u/my-app",
                "tool_name": "read_file",
                "tool_input": {"filePath": str(home / ".codex/skills/live-skill/SKILL.md")},
            }
            hook.run(tool_payload, data_dir=data_dir, home=home, agent="codex")

            events = store.read_events(data_dir)
            self.assertEqual(len(events), 2)
            self.assertEqual(events[0]["type"], "session")
            self.assertEqual(events[0]["agent"], "codex")
            self.assertEqual(events[1]["type"], "load")
            self.assertEqual(events[1]["agent"], "codex")
            self.assertEqual(events[1]["skill"], "live-skill")
            self.assertEqual(events[1]["source"], "agent-read")

    def test_codex_cli_reporting_and_install_hooks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            data_dir = root / "data"
            ws = root / "ws"
            touch(ws / ".codex/skills/codex-proj/SKILL.md")

            # Test install-hooks specifically for codex
            with patch("skill_usage.Path.home", return_value=home):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    code = skill_usage.main(["install-hooks", "--agent", "codex"])
                self.assertEqual(code, 0)
                codex_hooks_file = home / ".codex/hooks.json"
                self.assertTrue(codex_hooks_file.is_file())
                hooks_data = json.loads(codex_hooks_file.read_text(encoding="utf-8"))
                self.assertIn("SessionStart", hooks_data)
                self.assertIn("UserPromptSubmit", hooks_data)
                self.assertIn("PostToolUse", hooks_data)
                self.assertIn("--agent codex", hooks_data["SessionStart"][0]["hooks"][0]["command"])

                # Record an event for codex and run CLI report
                store.append_event(
                    {
                        "type": "load",
                        "ts": NOW.isoformat(),
                        "session_id": "c-sess",
                        "agent": "codex",
                        "skill": "codex-proj",
                        "source": "skill-tool",
                    },
                    data_dir,
                )
                csv_path = root / "codex-report.csv"
                rep_buf = io.StringIO()
                with contextlib.redirect_stdout(rep_buf):
                    code = skill_usage.main(
                        [
                            "report",
                            "--agent",
                            "codex",
                            "--data-dir",
                            str(data_dir),
                            "--workspace",
                            str(ws),
                            "--csv",
                            str(csv_path),
                        ]
                    )
                self.assertEqual(code, 0)
                output = rep_buf.getvalue()
                self.assertIn("# Codex skill load report", output)
                self.assertIn("codex-proj", output)
                self.assertTrue(csv_path.is_file())
                csv_text = csv_path.read_text(encoding="utf-8")
                self.assertIn("codex-proj", csv_text)

    def test_codex_sync_edge_cases_and_hook_preservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            data_dir = root / "data"
            touch(home / ".codex/skills/edge-skill/SKILL.md")

            # 1. Edge case logs: broken lines, session without session_meta, rapid read deduplication, bad json args
            log_path = home / ".codex/sessions/edge.jsonl"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            lines = [
                "not json",
                "123",
                json.dumps([]),
                # Message establishing session without session_meta
                json.dumps({
                    "timestamp": "2026-10-16T12:00:00Z",
                    "role": "user",
                    "content": "/edge-skill run test",
                }),
                # First read of edge-skill
                json.dumps({
                    "timestamp": "2026-10-16T12:00:10Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "read_file",
                        "arguments": json.dumps({"filePath": str(home / ".codex/skills/edge-skill/SKILL.md")}),
                    },
                }),
                # Second read within 20s (should be skipped by _should_skip_recent_read)
                json.dumps({
                    "timestamp": "2026-10-16T12:00:30Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "read_file",
                        "arguments": json.dumps({"filePath": str(home / ".codex/skills/edge-skill/SKILL.md")}),
                    },
                }),
                # Function call with malformed arguments string
                json.dumps({
                    "timestamp": "2026-10-16T12:00:40Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "read_file",
                        "arguments": "{bad json",
                    },
                }),
                # Plain user message without slash
                json.dumps({
                    "timestamp": "2026-10-16T12:00:50Z",
                    "role": "user",
                    "content": "just a regular message",
                }),
            ]
            log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

            events = sync.collect_codex_events(home, {"edge-skill"})
            # Expected: 1 session (derived from path stem "edge"), 1 user-slash, 1 agent-read (second read deduped)
            self.assertEqual(len(events), 3)
            self.assertEqual([e["type"] for e in events], ["session", "load", "load"])
            self.assertEqual(events[0]["session_id"], "edge")
            self.assertEqual([e["source"] for e in events if e["type"] == "load"], ["user-slash", "agent-read"])

            # 2. Codex log with standard OpenAI message dict and function_call
            touch(home / ".codex/skills/msg-skill/SKILL.md")
            msg_log_path = home / ".codex/sessions/msg_format.jsonl"
            msg_lines = [
                json.dumps({
                    "type": "session",
                    "id": "msg-sess-1",
                    "timestamp": "2026-10-16T13:00:00Z",
                    "cwd": "/Users/u/msg-proj",
                }),
                json.dumps({
                    "id": "msg-u1",
                    "type": "message",
                    "timestamp": "2026-10-16T13:00:05Z",
                    "message": {
                        "role": "user",
                        "content": "/msg-skill analyze",
                    },
                }),
                json.dumps({
                    "id": "msg-a1",
                    "type": "message",
                    "timestamp": "2026-10-16T13:00:10Z",
                    "message": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "tc-1",
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": json.dumps({"filePath": str(home / ".codex/skills/msg-skill/SKILL.md")}),
                                },
                            }
                        ],
                    },
                }),
                json.dumps({
                    "id": "msg-a2",
                    "type": "message",
                    "timestamp": "2026-10-16T13:00:20Z",
                    "message": {
                        "role": "assistant",
                        "function_call": {
                            "name": "skill",
                            "arguments": {"name": "msg-skill"},
                        },
                    },
                }),
            ]
            msg_log_path.write_text("\n".join(msg_lines) + "\n", encoding="utf-8")

            msg_events = sync.collect_codex_events(home, {"msg-skill"})
            sess_events = [e for e in msg_events if e["session_id"] == "msg-sess-1"]
            # Session + user-slash + agent-read + skill-tool = 4 events
            self.assertEqual(len(sess_events), 4)
            self.assertEqual(sess_events[0]["workspace"], "msg-proj")
            self.assertEqual(
                [(e["type"], e.get("source")) for e in sess_events],
                [("session", None), ("load", "user-slash"), ("load", "agent-read"), ("load", "skill-tool")],
            )

            # 3. Hook preservation: pre-existing custom hooks in ~/.codex/hooks.json (including 'hooks' envelope)
            codex_hooks = home / ".codex/hooks.json"
            codex_hooks.parent.mkdir(parents=True, exist_ok=True)
            existing_config = {
                "hooks": {
                    "PostToolUse": [
                        {"hooks": [{"type": "command", "command": "run-custom-linter.sh"}]}
                    ]
                }
            }
            codex_hooks.write_text(json.dumps(existing_config, indent=2), encoding="utf-8")

            # First install preserves 'hooks' envelope
            sync.install_hooks(home=home, agents=["codex"])
            loaded = json.loads(codex_hooks.read_text(encoding="utf-8"))
            self.assertIn("hooks", loaded)
            post_tools = loaded["hooks"]["PostToolUse"]
            cmds = [h["command"] for entry in post_tools for h in entry.get("hooks", [])]
            self.assertIn("run-custom-linter.sh", cmds)
            self.assertTrue(any("hook.py" in c and "--agent codex" in c for c in cmds))

            # Second install: idempotent, does not duplicate hook.py
            sync.install_hooks(home=home, agents=["codex"])
            loaded_again = json.loads(codex_hooks.read_text(encoding="utf-8"))
            hook_py_count = sum(
                1
                for entry in loaded_again["hooks"]["PostToolUse"]
                for h in entry.get("hooks", [])
                if "hook.py" in h.get("command", "")
            )
            self.assertEqual(hook_py_count, 1)

    def test_codex_sync_ignores_history_and_index_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            touch(home / ".codex/skills/test-skill/SKILL.md")

            # 1. history.jsonl with prompt
            hist_file = home / ".codex/history/history.jsonl"
            touch(
                hist_file,
                json.dumps({"timestamp": "2026-10-16T10:00:00Z", "role": "user", "content": "/test-skill run"}) + "\n",
            )
            # 2. session_index.jsonl
            index_file = home / ".codex/session_index.jsonl"
            touch(
                index_file,
                json.dumps({"id": "index-1", "title": "Check code", "timestamp": "2026-10-16T10:00:00Z"}) + "\n",
            )

            events = sync.collect_codex_events(home, {"test-skill"})
            # Neither history.jsonl nor session_index.jsonl should produce any events
            self.assertEqual(len(events), 0)

    def test_codex_sync_supports_json_array_and_content_tool_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            data_dir = root / "data"
            touch(home / ".codex/skills/array-skill/SKILL.md")

            # Session stored as JSON array with millisecond epoch timestamp and assistant content tool call
            # Notice cwd is specified on line 2 (tool call), not line 1 (user message)
            sess_file = home / ".codex/sessions/2026/10/16/session_array.json"
            sess_data = [
                {
                    "timestamp": 1792140000000,  # Milliseconds epoch (~2026-10-16)
                    "role": "user",
                    "content": "/array-skill run test",
                },
                {
                    "timestamp": 1792140005000,
                    "cwd": "/Users/u/my-workspace",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "read_file",
                            "input": {"filepath": str(home / ".codex/skills/array-skill/SKILL.md")},
                        }
                    ],
                },
            ]
            touch(sess_file, json.dumps(sess_data, indent=2))

            counts = sync.sync_events(data_dir=data_dir, home=home, workspaces=[])
            self.assertEqual(counts["codex"], 3)  # session + user-slash + agent-read

            events = store.read_events(data_dir)
            self.assertEqual(len(events), 3)
            # Workspace must be backfilled to all events in the session
            self.assertEqual(events[0]["type"], "session")
            self.assertEqual(events[0]["workspace"], "my-workspace")
            self.assertEqual(events[1]["type"], "load")
            self.assertEqual(events[1]["source"], "user-slash")
            self.assertEqual(events[1]["workspace"], "my-workspace")
            self.assertEqual(events[2]["type"], "load")
            self.assertEqual(events[2]["source"], "agent-read")
            self.assertEqual(events[2]["workspace"], "my-workspace")

    def test_detect_handles_content_offset_uppercase_and_tool_path_inference(self):
        # 1. view_file with ContentOffset > 0 is recognized as pagination continuation and ignored
        paginated_event = {
            "hook_event_name": "PostToolUse",
            "timestamp": TS,
            "session_id": "codex-pg-1",
            "agent": "codex",
            "tool_name": "view_file",
            "tool_input": {
                "AbsolutePath": "/Users/u/.codex/skills/my-skill/SKILL.md",
                "ContentOffset": 46080,
            },
        }
        self.assertIsNone(detect.detect(paginated_event, set))

        # 2. Uppercase skill name in slash prompt and Skill tool
        upper_slash = {
            "hook_event_name": "UserPromptSubmit",
            "timestamp": TS,
            "session_id": "codex-up-1",
            "agent": "codex",
            "prompt": "/Code-Review inspect diff",
        }
        rec_slash = detect.detect(upper_slash, lambda: {"Code-Review"})
        self.assertIsNotNone(rec_slash)
        self.assertEqual(rec_slash["skill"], "Code-Review")
        self.assertEqual(rec_slash["source"], "user-slash")

        upper_tool = {
            "hook_event_name": "PostToolUse",
            "timestamp": TS,
            "session_id": "codex-up-1",
            "agent": "codex",
            "tool_name": "Skill",
            "tool_input": {"skill": "Code-Review"},
        }
        rec_tool = detect.detect(upper_tool, set)
        self.assertIsNotNone(rec_tool)
        self.assertEqual(rec_tool["skill"], "Code-Review")
        self.assertEqual(rec_tool["source"], "skill-tool")

        # 3. Path-based agent inference from tool_input reading ~/.codex/skills
        unspecified_agent_event = {
            "hook_event_name": "PostToolUse",
            "timestamp": TS,
            "session_id": "codex-inf-1",
            "tool_name": "read_file",
            "tool_input": {
                "filename": "/Users/u/.codex/skills/Code-Review/SKILL.md",
            },
        }
        rec_inf = detect.detect(unspecified_agent_event, set)
        self.assertIsNotNone(rec_inf)
        self.assertEqual(rec_inf["agent"], "codex")
        self.assertEqual(rec_inf["skill"], "Code-Review")

        # 4. Prompt normalization with alternate header casing
        alt_header_event = {
            "hook_event_name": "UserPromptSubmit",
            "timestamp": TS,
            "session_id": "codex-inf-1",
            "agent": "codex",
            "prompt": "### My request for Codex:\n/Code-Review check",
        }
        rec_alt = detect.detect(alt_header_event, lambda: {"Code-Review"})
        self.assertIsNotNone(rec_alt)
        self.assertEqual(rec_alt["skill"], "Code-Review")

    def test_codex_sync_pre_resolves_sid_and_cwd_preventing_duplicate_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            data_dir = root / "data"
            touch(home / ".codex/skills/review-flow/SKILL.md")

            rollout = home / ".codex/sessions/rollout-2026-10-16.jsonl"
            lines = [
                # line 1: user prompt arrives before session_meta
                json.dumps({"type": "user_message", "timestamp": 1792140000, "message": {"content": "/review-flow run"}}),
                # line 2: session_meta arrives later with true session id and cwd
                json.dumps({"type": "session_meta", "timestamp": 1792140001, "payload": {"id": "real-uuid-456", "cwd": "/home/u/work"}}),
                # line 3: assistant reads the skill file
                json.dumps({
                    "type": "response_item",
                    "timestamp": 1792140005,
                    "payload": {
                        "type": "function_call",
                        "function": {
                            "name": "read_file",
                            "arguments": json.dumps({"path": str(home / ".codex/skills/review-flow/SKILL.md")}),
                        },
                    },
                }),
            ]
            touch(rollout, "\n".join(lines) + "\n")

            counts = sync.sync_events(data_dir=data_dir, home=home, workspaces=[])
            self.assertEqual(counts["codex"], 3)  # 1 session + 1 user-slash + 1 agent-read

            events = store.read_events(data_dir)
            session_events = [e for e in events if e.get("type") == "session"]
            self.assertEqual(len(session_events), 1)
            self.assertEqual(session_events[0]["session_id"], "real-uuid-456")
            self.assertEqual(session_events[0]["workspace"], "work")

            load_events = [e for e in events if e.get("type") == "load"]
            self.assertEqual(len(load_events), 2)
            self.assertEqual(load_events[0]["session_id"], "real-uuid-456")
            self.assertEqual(load_events[0]["workspace"], "work")
            self.assertEqual(load_events[1]["session_id"], "real-uuid-456")
            self.assertEqual(load_events[1]["workspace"], "work")

            # Report summarize should pair the user-slash and agent-read cleanly
            copies = inventory.scan(home, [], agent="codex")
            summary = report.summarize(events, copies, NOW, 30, agent="codex")
            self.assertEqual(summary.merged, 1)
            self.assertEqual(len(summary.sessions), 1)
            self.assertEqual(summary.rows[0].total, 1)

    def test_codex_sync_extracts_nested_openai_function_and_tool_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            touch(home / ".codex/skills/call-skill/SKILL.md")

            rollout = home / ".codex/sessions/rollout-call.jsonl"
            lines = [
                json.dumps({"type": "session_meta", "timestamp": 1792140000, "payload": {"id": "call-sess-1", "cwd": "/ws"}}),
                # Nested payload.function
                json.dumps({
                    "type": "response_item",
                    "timestamp": 1792140002,
                    "payload": {
                        "type": "function_call",
                        "function": {
                            "name": "read_file",
                            "arguments": json.dumps({"path": str(home / ".codex/skills/call-skill/SKILL.md")}),
                        },
                    },
                }),
                # Nested call.call
                json.dumps({
                    "type": "tool_call",
                    "timestamp": 1792140070,
                    "call": {
                        "name": "readFile",
                        "args": {"target_file": str(home / ".codex/skills/call-skill/SKILL.md")},
                    },
                }),
            ]
            touch(rollout, "\n".join(lines) + "\n")

            events = sync.collect_codex_events(home, {"call-skill"})
            load_events = [e for e in events if e.get("type") == "load"]
            self.assertEqual(len(load_events), 2)
            self.assertEqual(load_events[0]["skill"], "call-skill")
            self.assertEqual(load_events[1]["skill"], "call-skill")

    def test_codex_sync_respects_relative_path_exclusion_under_cache_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Home directory located inside a path component named 'cache'
            home = Path(tmp) / "cache" / "dev_home"
            touch(home / ".codex/skills/cache-skill/SKILL.md")
            rollout = home / ".codex/sessions/rollout-cache.jsonl"
            touch(
                rollout,
                json.dumps({"type": "session_meta", "timestamp": 1792140000, "payload": {"id": "c-1"}}) + "\n"
                + json.dumps({"type": "user_message", "timestamp": 1792140001, "message": {"content": "/cache-skill"}}) + "\n",
            )

            events = sync.collect_codex_events(home, {"cache-skill"})
            self.assertEqual(len(events), 2)
            self.assertEqual(events[0]["session_id"], "c-1")
            self.assertEqual(events[1]["skill"], "cache-skill")

    def test_codex_sync_unpacks_json_with_items_list_and_top_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            touch(home / ".codex/skills/items-skill/SKILL.md")
            sess_file = home / ".codex/sessions/session_items.json"
            touch(
                sess_file,
                json.dumps({
                    "id": "items-sess-99",
                    "cwd": "/workspace/my-app",
                    "items": [
                        {"type": "user_message", "timestamp": 1792140000, "message": {"content": "/items-skill test"}},
                    ],
                }),
            )

            events = sync.collect_codex_events(home, {"items-skill"})
            self.assertEqual(len(events), 2)
            self.assertEqual(events[0]["type"], "session")
            self.assertEqual(events[0]["session_id"], "items-sess-99")
            self.assertEqual(events[0]["workspace"], "my-app")
            self.assertEqual(events[1]["type"], "load")
            self.assertEqual(events[1]["skill"], "items-skill")
            self.assertEqual(events[1]["workspace"], "my-app")

    def test_detect_handles_relative_path_and_slash_namespaced_tools(self):
        # 1. Relative path in tool_input infers agent as codex
        rel_event = {
            "hook_event_name": "PostToolUse",
            "timestamp": TS,
            "session_id": "s-rel",
            "tool_name": "read_file",
            "tool_input": {
                "path": ".codex/skills/my-skill/SKILL.md",
            },
        }
        rec_rel = detect.detect(rel_event, set)
        self.assertIsNotNone(rec_rel)
        self.assertEqual(rec_rel["agent"], "codex")
        self.assertEqual(rec_rel["skill"], "my-skill")

        # 2. absolute_path key in tool_input
        abs_event = {
            "hook_event_name": "PostToolUse",
            "timestamp": TS,
            "session_id": "s-abs",
            "agent": "codex",
            "tool_name": "read_file",
            "tool_input": {
                "absolute_path": "/Users/u/.codex/skills/my-skill/SKILL.md",
            },
        }
        rec_abs = detect.detect(abs_event, set)
        self.assertIsNotNone(rec_abs)
        self.assertEqual(rec_abs["skill"], "my-skill")

        # 3. Namespaced tool with slash and dot (e.g. codex/read_file, openai.read_file)
        slash_tool_event = {
            "hook_event_name": "PostToolUse",
            "timestamp": TS,
            "session_id": "s-slash",
            "agent": "codex",
            "tool_name": "codex/read_file",
            "tool_input": {
                "path": "/Users/u/.codex/skills/my-skill/SKILL.md",
            },
        }
        rec_slash = detect.detect(slash_tool_event, set)
        self.assertIsNotNone(rec_slash)
        self.assertEqual(rec_slash["skill"], "my-skill")

        # 4. Numeric epoch timestamp is converted and preserved
        epoch_event = {
            "hook_event_name": "SessionStart",
            "timestamp": 1792140000,
            "session_id": "epoch-sess",
            "agent": "codex",
        }
        rec_epoch = detect.detect(epoch_event, set)
        self.assertIsNotNone(rec_epoch)
        self.assertEqual(rec_epoch["ts"], "2026-10-16T08:40:00+00:00")


if __name__ == "__main__":
    unittest.main()


