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
                hooks_json = (home / ".gemini/config/hooks.json").read_text(encoding="utf-8")
                self.assertIn("--agent antigravity", hooks_json)
                self.assertTrue((home / ".pi/agent/extensions/skill-usage.ts").is_file())
                hermes_plugin = (home / ".hermes/plugins/skill-usage/__init__.py").read_text(encoding="utf-8")
                self.assertIn("on_user_prompt", hermes_plugin)

                sync_buf = io.StringIO()
                with contextlib.redirect_stdout(sync_buf):
                    code = skill_usage.main(["sync", "--data-dir", str(data_dir)])
                self.assertEqual(code, 0)
                self.assertIn("Synced 0 new events", sync_buf.getvalue())

    def test_pi_sync_handles_json_string_arguments(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            session_dir = home / ".pi" / "agent" / "sessions" / "project"
            session_dir.mkdir(parents=True)
            session_file = session_dir / "session-1.jsonl"

            log_line = json.dumps(
                {
                    "type": "message",
                    "timestamp": "2026-10-18T12:00:00Z",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "toolCall",
                                "name": "read",
                                "arguments": json.dumps({"path": "/Users/user/.pi/agent/skills/analyzer/SKILL.md"}),
                            }
                        ],
                    },
                }
            )
            session_file.write_text(log_line + "\n", encoding="utf-8")

            events = sync.collect_pi_events(home, {"analyzer"})
            load_events = [e for e in events if e.get("type") == "load"]
            self.assertEqual(len(load_events), 1)
            self.assertEqual(load_events[0]["skill"], "analyzer")
            self.assertEqual(load_events[0]["agent"], "pi")
            self.assertEqual(load_events[0]["source"], "agent-read")

    def test_inventory_scans_pi_npm_package_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            skill_dir = home / ".pi" / "agent" / "npm" / "agent-pack" / "skills" / "linter"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("# Linter Skill\n", encoding="utf-8")

            copies = inventory.scan(home, [])
            matched = [c for c in copies if c.name == "linter"]
            self.assertEqual(len(matched), 1)
            self.assertEqual(matched[0].source, "plugin:pi/agent-pack")
            self.assertEqual(matched[0].agents, ("pi",))

    def test_antigravity_ide_brain_synchronization(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            log_dir = home / ".gemini" / "antigravity-ide" / "brain" / "conv-ide-42" / ".system_generated" / "logs"
            log_dir.mkdir(parents=True)
            transcript = log_dir / "transcript.jsonl"

            entry = json.dumps(
                {
                    "type": "PLANNER_RESPONSE",
                    "created_at": "2026-10-18T08:00:00Z",
                    "tool_calls": [
                        {
                            "name": "view_file",
                            "args": {"AbsolutePath": "/Users/user/.gemini/config/skills/refactor/SKILL.md"},
                        }
                    ],
                }
            )
            transcript.write_text(entry + "\n", encoding="utf-8")

            events = sync.collect_antigravity_events(home, {"refactor"})
            load_events = [e for e in events if e.get("type") == "load"]
            self.assertEqual(len(load_events), 1)
            self.assertEqual(load_events[0]["session_id"], "conv-ide-42")
            self.assertEqual(load_events[0]["skill"], "refactor")
            self.assertEqual(load_events[0]["agent"], "antigravity")

    def test_claude_sync_and_plugin_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            plugin_install = home / ".claude" / "plugins" / "custom-pack"
            (plugin_install / "skills" / "claude-pack-skill").mkdir(parents=True)
            (plugin_install / "skills" / "claude-pack-skill" / "SKILL.md").write_text("# Skill\n", encoding="utf-8")
            manifest = home / ".claude" / "plugins" / "installed_plugins.json"
            manifest.write_text(
                json.dumps({"plugins": {"my-pack@latest": [{"installPath": str(plugin_install)}]}}),
                encoding="utf-8",
            )
            cache_skill = home / ".claude" / "plugins" / "cache" / "market" / "cached-plug" / "1.0.0" / "skills" / "cached-skill"
            cache_skill.mkdir(parents=True)
            (cache_skill / "SKILL.md").write_text("# Cached\n", encoding="utf-8")

            copies = inventory.scan(home, [], agent="claude")
            by_name = {c.name: c.source for c in copies}
            self.assertEqual(by_name.get("claude-pack-skill"), "plugin:claude/my-pack")
            self.assertEqual(by_name.get("cached-skill"), "plugin:claude/cached-plug")

            proj_dir = home / ".claude" / "projects" / "my-proj"
            proj_dir.mkdir(parents=True)
            transcript = proj_dir / "sess-claude-1.jsonl"
            lines = [
                json.dumps(
                    {
                        "sessionId": "sess-claude-1",
                        "cwd": "/work/my-proj",
                        "timestamp": "2026-10-18T09:00:00Z",
                        "message": {"role": "user", "content": "<command-name>/cached-skill</command-name>"},
                    }
                ),
                json.dumps(
                    {
                        "sessionId": "sess-claude-1",
                        "cwd": "/work/my-proj",
                        "timestamp": "2026-10-18T09:05:00Z",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "name": "Read",
                                    "input": {"file_path": str(cache_skill / "SKILL.md")},
                                }
                            ],
                        },
                    }
                ),
            ]
            transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")

            events = sync.collect_claude_events(home, {"cached-skill", "claude-pack-skill"})
            loads = [e for e in events if e.get("type") == "load"]
            self.assertEqual(len(loads), 2)
            self.assertEqual({e["source"] for e in loads}, {"user-slash", "agent-read"})
            self.assertEqual({e["agent"] for e in loads}, {"claude"})

    def test_hermes_sqlite_locked_graceful_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            hermes_dir = home / ".hermes"
            hermes_dir.mkdir(parents=True)
            db_path = hermes_dir / "state.db"
            db_path.touch()

            with patch("sqlite3.connect", side_effect=sqlite3.OperationalError("database is locked")):
                events = sync.collect_hermes_events(home, {"test-skill"})
                self.assertEqual(events, [])

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


if __name__ == "__main__":
    unittest.main()
