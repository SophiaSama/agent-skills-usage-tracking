import csv
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

import skill_usage
from tracker import dashboard, evals, report, store
from tracker.inventory import SkillCopy

NOW = datetime(2026, 11, 20, tzinfo=timezone.utc)


def _make_skill(root: Path, name: str, *, description: str = "A helpful skill for testing workflows", link: str | None = None, with_evals: bool = False) -> SkillCopy:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    body = f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n"
    if link:
        body += f"\nSee [ref]({link}).\n"
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(body, encoding="utf-8")
    if with_evals:
        eval_dir = skill_dir / "evals"
        eval_dir.mkdir(exist_ok=True)
        (eval_dir / "evals.json").write_text(
            json.dumps(
                {
                    "skill_name": name,
                    "evals": [
                        {
                            "id": 1,
                            "prompt": "Format greeting",
                            "assertions": [{"type": "contains", "value": "HELLO_FROM_SKILL"}],
                        },
                        {
                            "id": 2,
                            "prompt": "Return JSON",
                            "assertions": [{"type": "json_valid"}],
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
    return SkillCopy(name, str(skill_md), "personal", ("copilot", "claude"))


class EvalTests(unittest.TestCase):
    def test_store_eval_roundtrip_and_latest_by_skill(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            store.append_eval({"type": "eval", "ts": "2026-11-01T00:00:00+00:00", "skill": "alpha", "agent": "claude", "with_skill_rate": 0.5, "benefit_delta": 0.1}, data_dir)
            store.append_eval({"type": "eval", "ts": "2026-11-10T00:00:00+00:00", "skill": "Alpha", "agent": "claude", "with_skill_rate": 1.0, "benefit_delta": 0.5}, data_dir)
            store.append_eval({"type": "eval", "ts": "2026-11-12T00:00:00+00:00", "skill": "alpha", "agent": "codex", "with_skill_rate": 0.75, "benefit_delta": 0.25}, data_dir)
            with open(data_dir / "evals-2026-11.jsonl", "a", encoding="utf-8") as fh:
                fh.write("{bad json\n")

            records = store.read_evals(data_dir)
            self.assertEqual(len(records), 3)

            latest_all = store.latest_evals_by_skill(records)
            self.assertEqual(latest_all["alpha"]["with_skill_rate"], 0.75)

            latest_claude = store.latest_evals_by_skill(records, agent="claude")
            self.assertEqual(latest_claude["alpha"]["with_skill_rate"], 1.0)

    def test_lint_skill_checks_frontmatter_and_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            good = _make_skill(root, "good-skill", with_evals=True)
            res_good = evals.lint_skill(good)
            self.assertTrue(res_good.ok)
            self.assertTrue(res_good.has_provider_evals)

            bad = _make_skill(root, "bad-skill", description="short", link="references/missing.md")
            res_bad = evals.lint_skill(bad)
            self.assertFalse(res_bad.ok)
            self.assertTrue(any("short" in issue for issue in res_bad.issues))
            self.assertTrue(any("missing.md" in issue for issue in res_bad.issues))

    def test_discover_eval_file_prefers_user_override_over_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            copy = _make_skill(root / "skills", "my-skill", with_evals=True)
            user_dir = root / "user-evals"
            user_dir.mkdir()
            user_file = user_dir / "my-skill.json"
            user_file.write_text('{"evals": []}', encoding="utf-8")

            found, source = evals.discover_eval_file("my-skill", [copy], evals_dir=user_dir)
            self.assertEqual(found, user_file)
            self.assertEqual(source, "user")

            user_file.unlink()
            found2, source2 = evals.discover_eval_file("my-skill", [copy], evals_dir=user_dir)
            self.assertEqual(source2, "provider")
            self.assertTrue(str(found2).endswith("evals/evals.json"))

    def test_grade_assertions_all_types(self):
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            (workdir / "out.txt").write_text("artifact content", encoding="utf-8")
            assertions = [
                {"type": "contains", "value": "hello"},
                {"type": "not_contains", "value": "error"},
                {"type": "regex", "pattern": r'"id":\s*\d+'},
                {"type": "file_exists", "path": "out.txt"},
                {"type": "file_contains", "path": "out.txt", "value": "artifact"},
                {"type": "json_valid"},
                {"type": "command", "cmd": f"{sys.executable} -c \"import os; assert 'hello' in os.environ['EVAL_OUTPUT']\""},
            ]
            output = '{"msg": "hello", "id": 42}'
            passed, total, _ = evals.grade_assertions(output, workdir, assertions)
            self.assertEqual(passed, 7)
            self.assertEqual(total, 7)

    def test_run_skill_eval_ab_computes_benefit_delta_and_privacy_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            copy = _make_skill(root, "greeter", with_evals=True)

            def mock_runner(prompt: str, workdir: Path, skill_text: str | None) -> str:
                if "JSON" in prompt:
                    return '{"ok": true}'
                if skill_text is not None:
                    return "HELLO_FROM_SKILL"
                return "plain greeting"

            res = evals.run_skill_eval("greeter", [copy], runner=mock_runner, runner_label="mock", now=NOW)
            self.assertEqual(res.cases_total, 2)
            self.assertEqual(res.with_skill_passed, 2)
            self.assertEqual(res.baseline_passed, 1)
            self.assertEqual(res.with_skill_rate, 1.0)
            self.assertEqual(res.baseline_rate, 0.5)
            self.assertEqual(res.benefit_delta, 0.5)

            rec = evals.eval_record_from_run(res)
            self.assertEqual(rec["skill"], "greeter")
            self.assertEqual(rec["benefit_delta"], 0.5)
            self.assertNotIn("prompt", rec)

    def test_run_skill_eval_dry_run_and_judge_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            copy = _make_skill(root, "judge-skill")
            eval_path = root / "custom-eval.json"
            eval_path.write_text(
                json.dumps(
                    {
                        "evals": [
                            {"id": "c1", "prompt": "Explain X", "expectations": ["Mentions X clearly"]},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            dry = evals.run_skill_eval("judge-skill", [copy], eval_file=eval_path, dry_run=True, now=NOW)
            self.assertTrue(dry.dry_run)
            self.assertEqual(dry.cases_total, 1)
            self.assertIsNone(dry.with_skill_rate)

            res = evals.run_skill_eval(
                "judge-skill",
                [copy],
                eval_file=eval_path,
                runner=lambda p, w, s: "X is clear" if s else "vague",
                judge=lambda out, exp, eo: ("clear" in out, "checked"),
                now=NOW,
            )
            self.assertEqual(res.with_skill_rate, 1.0)
            self.assertEqual(res.baseline_rate, 0.0)
            self.assertEqual(res.benefit_delta, 1.0)

    def test_report_and_dashboard_include_eval_scores_and_usefulness(self):
        copies = [
            SkillCopy("core-skill", "/p/core-skill/SKILL.md", "personal"),
            SkillCopy("hidden-gem", "/p/hidden-gem/SKILL.md", "personal"),
            SkillCopy("noisy-skill", "/p/noisy-skill/SKILL.md", "personal"),
        ]
        events = [
            {"type": "session", "ts": (NOW - timedelta(days=2)).isoformat(), "session_id": "s1"},
            {"type": "load", "ts": (NOW - timedelta(days=2)).isoformat(), "session_id": "s1", "skill": "core-skill", "source": "agent-read"},
            {"type": "load", "ts": (NOW - timedelta(days=1)).isoformat(), "session_id": "s2", "skill": "noisy-skill", "source": "agent-read"},
        ]
        eval_records = [
            {"type": "eval", "ts": NOW.isoformat(), "skill": "core-skill", "eval_source": "provider", "with_skill_rate": 0.9, "baseline_rate": 0.4, "benefit_delta": 0.5, "cases_total": 10},
            {"type": "eval", "ts": NOW.isoformat(), "skill": "hidden-gem", "eval_source": "user", "with_skill_rate": 1.0, "baseline_rate": 0.5, "benefit_delta": 0.5, "cases_total": 4},
            {"type": "eval", "ts": NOW.isoformat(), "skill": "noisy-skill", "eval_source": "provider", "with_skill_rate": 0.6, "baseline_rate": 0.6, "benefit_delta": 0.0, "cases_total": 5},
        ]

        summary = report.summarize(events, copies, NOW, 30, evals=eval_records)
        self.assertTrue(summary.has_evals)
        by_name = {r.name: r for r in summary.rows}
        self.assertEqual(report.usefulness_label(by_name["core-skill"], False), "high-value")
        self.assertEqual(report.usefulness_label(by_name["hidden-gem"], False), "under-triggered")
        self.assertEqual(report.usefulness_label(by_name["noisy-skill"], False), "low-benefit")

        md = report.render(summary, 30, 20)
        self.assertIn("| Eval | Benefit |", md)
        self.assertIn("90%", md)
        self.assertIn("+50%", md)
        self.assertIn("under-triggered", md)

        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "out.csv"
            report.write_csv(summary, csv_path, False)
            with open(csv_path, newline="", encoding="utf-8") as fh:
                csv_rows = {row["skill"]: row for row in csv.DictReader(fh)}
            self.assertEqual(csv_rows["core-skill"]["usefulness"], "high-value")
            self.assertEqual(csv_rows["hidden-gem"]["benefit_delta"], "0.5")

        dash_data = dashboard.build_dashboard_data(events, copies, NOW, evals=eval_records)
        self.assertEqual(dash_data["ranges"]["all"]["evalSummary"]["evaluatedSkills"], 3)
        skills_map = {s["name"]: s for s in dash_data["ranges"]["all"]["skills"]}
        self.assertEqual(skills_map["core-skill"]["eval"]["delta"], 0.5)
        self.assertEqual(skills_map["hidden-gem"]["usefulness"], "under-triggered")

        html = dashboard.render_html(dash_data)
        self.assertIn('data-filter="evaluated"', html)
        self.assertIn("Eval score", html)

    def test_cli_eval_record_and_lint_and_runner_cmd(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = root / "ws"
            data_dir = root / "data"
            _make_skill(ws / ".github" / "skills", "demo-skill", with_evals=True)

            # 1. Lint-only check
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = skill_usage.main(["eval", "--lint-only", "--skill", "demo-skill", "--workspace", str(ws)])
            self.assertEqual(rc, 0)
            self.assertIn("demo-skill: OK [has evals]", buf.getvalue())

            # 2. Run eval using python as a custom runner-cmd
            runner_script = root / "mock_agent.py"
            runner_script.write_text(
                "import sys\n"
                "prompt = sys.argv[1]\n"
                "if 'JSON' in prompt:\n"
                "    print('{\"ok\": true}')\n"
                "elif '<skill_instructions>' in prompt:\n"
                "    print('HELLO_FROM_SKILL')\n"
                "else:\n"
                "    print('baseline')\n",
                encoding="utf-8",
            )
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = skill_usage.main([
                    "eval",
                    "--skill", "demo-skill",
                    "--workspace", str(ws),
                    "--data-dir", str(data_dir),
                    "--runner-cmd", f"{sys.executable} {runner_script} {{prompt}}",
                ])
            self.assertEqual(rc, 0)
            self.assertIn("2/2 passed (100%, baseline 50%, benefit +50%)", buf.getvalue())

            # 3. Verify eval-record + report integration
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = skill_usage.main([
                    "eval-record",
                    "--skill", "manual-skill",
                    "--score", "0.8",
                    "--baseline", "0.2",
                    "--data-dir", str(data_dir),
                ])
            self.assertEqual(rc, 0)
            records = store.read_evals(data_dir)
            self.assertEqual(len(records), 2)


if __name__ == "__main__":
    unittest.main()
