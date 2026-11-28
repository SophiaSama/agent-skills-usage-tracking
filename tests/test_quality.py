import csv
import json
import tempfile
import unittest
from pathlib import Path

from tracker import inventory, quality
from tracker.inventory import SkillCopy


def write_text(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


class QualityAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def make_skill(
        self,
        name: str,
        skill_md: str,
        source: str = "personal",
        agents: tuple[str, ...] = ("antigravity",),
        subdir: str = "skills",
    ) -> tuple[Path, SkillCopy]:
        skill_dir = self.root / subdir / name
        md_path = write_text(skill_dir / "SKILL.md", skill_md)
        return skill_dir, SkillCopy(name, str(md_path), source, agents)

    # ------------------------------------------------------------------
    # Frontmatter parsing & multiline scalars

    def test_parses_folded_and_literal_multiline_yaml_frontmatter(self):
        _, folded_copy = self.make_skill(
            "folded-skill",
            (
                "---\n"
                "name: folded-skill\n"
                "description: >-\n"
                "  Use this skill when auditing local agent configurations\n"
                "  and generating structured review summaries.\n"
                "---\n"
                "# Folded Skill\n"
            ),
        )
        parsed_folded = quality.parse_skill_md(folded_copy.path)
        self.assertTrue(parsed_folded.has_frontmatter)
        self.assertEqual(parsed_folded.frontmatter["name"], "folded-skill")
        self.assertEqual(
            parsed_folded.frontmatter["description"],
            "Use this skill when auditing local agent configurations and generating structured review summaries.",
        )

        _, literal_copy = self.make_skill(
            "literal-skill",
            (
                "---\n"
                'name: "literal-skill"\n'
                "description: |\n"
                "  Build and validate standalone HTML reports.\n"
                "  Use when exporting offline dashboards.\n"
                "---\n"
                "Body text.\n"
            ),
        )
        parsed_literal = quality.parse_skill_md(literal_copy.path)
        self.assertTrue(parsed_literal.has_frontmatter)
        self.assertEqual(parsed_literal.frontmatter["name"], "literal-skill")
        self.assertIn("Build and validate standalone HTML reports.\nUse when exporting offline dashboards.", parsed_literal.frontmatter["description"])

        summary = quality.audit([folded_copy, literal_copy])
        self.assertEqual(summary.clean_skills, 2)
        self.assertEqual(summary.warning_skills, 0)

    def test_parses_quoted_and_plain_indented_frontmatter_and_handles_oserror(self):
        _, quoted = self.make_skill(
            "quoted-skill",
            (
                "---\n"
                "name: 'quoted-skill'\n"
                'description: "Run database migrations and verify schema invariants across environments."\n'
                "---\n"
                "# Quoted\n"
            ),
        )
        _, plain_indented = self.make_skill(
            "indented-skill",
            (
                "---\n"
                "name: indented-skill\n"
                "description:\n"
                "  Inspect transcript logs when debugging missed tool calls\n"
                "  across local sessions.\n"
                "metadata:\n"
                "  owner: team\n"
                "---\n"
                "# Indented\n"
            ),
        )
        summary = quality.audit([quoted, plain_indented])
        self.assertEqual(summary.clean_skills, 2)

        missing_copy = SkillCopy("synthetic", str(self.root / "nonexistent" / "SKILL.md"), "personal")
        parsed_missing = quality.parse_skill_md(missing_copy.path)
        self.assertFalse(parsed_missing.readable)
        self.assertEqual(quality.inspect_copy(missing_copy), [])

    def test_missing_and_unclosed_frontmatter_reported_as_warning(self):
        _, no_fm = self.make_skill("bare-md", "# Just a heading\n\nSome instructions.\n")
        _, unclosed_fm = self.make_skill(
            "unclosed-fm",
            "---\nname: unclosed-fm\ndescription: Use when testing unclosed YAML frontmatter blocks.\n# No closing fence\n",
        )
        summary = quality.audit([no_fm, unclosed_fm])
        by_name = {r.name: r for r in summary.rows}
        self.assertEqual([i.code for i in by_name["bare-md"].issues], ["missing-frontmatter"])
        self.assertEqual([i.code for i in by_name["unclosed-fm"].issues], ["missing-frontmatter"])
        self.assertFalse(by_name["bare-md"].ok)

    # ------------------------------------------------------------------
    # Name, description & SKILL.md size checks

    def test_missing_name_and_missing_description(self):
        _, copy = self.make_skill("incomplete", "---\n---\n# Body\n")
        codes = [i.code for i in quality.inspect_copy(copy)]
        self.assertEqual(codes, ["missing-name", "missing-description"])

    def test_concise_action_descriptions_pass_while_short_or_cueless_fail(self):
        # Concise >= 40 char action-oriented descriptions should NOT trigger false positives
        _, concise_ok = self.make_skill(
            "concise-ok",
            "---\nname: concise-ok\ndescription: Run unit tests and check coverage across packages.\n---\n# Test\n",
        )
        self.assertEqual(quality.inspect_copy(concise_ok), [])

        # < 40 chars -> weak-description even if it has a verb
        _, too_short = self.make_skill(
            "too-short",
            "---\nname: too-short\ndescription: Run tests quickly.\n---\n# Short\n",
        )
        self.assertEqual([i.code for i in quality.inspect_copy(too_short)], ["weak-description"])

        # 40..79 chars with zero trigger/action cues -> weak-description
        _, no_cues = self.make_skill(
            "no-cues",
            "---\nname: no-cues\ndescription: Internal collection of miscellaneous static notes and constants.\n---\n# Notes\n",
        )
        self.assertEqual([i.code for i in quality.inspect_copy(no_cues)], ["weak-description"])

    def test_overlong_description_and_oversized_skill_md(self):
        long_desc = "Use this skill when validating schemas. " + (" detailed specification" * 50)
        self.assertGreater(len(long_desc), 1024)
        body_lines = "\n".join(f"Line {idx}" for idx in range(505))
        _, bloated = self.make_skill(
            "bloated",
            f"---\nname: bloated\ndescription: {long_desc}\n---\n{body_lines}\n",
        )
        codes = [i.code for i in quality.inspect_copy(bloated)]
        self.assertIn("overlong-description", codes)
        self.assertIn("oversized-skill-md", codes)

    # ------------------------------------------------------------------
    # Broken vs valid links & code-span exclusions

    def test_broken_vs_valid_relative_and_file_links_and_ignored_schemes(self):
        external_doc = write_text(self.root / "shared" / "guide.md", "# Shared guide\n")
        skill_dir, copy = self.make_skill(
            "linker",
            (
                "---\n"
                "name: linker\n"
                "description: Use when checking local markdown links and external URI schemes.\n"
                "---\n"
                "Valid relative: [api](references/api.md#auth)\n"
                f"Valid file URI: [shared]({external_doc.as_uri()})\n"
                "Ignored schemes: [web](https://example.com) [http](http://example.com) "
                "[mail](mailto:dev@example.com) [sidecar](sidecar://panel) "
                "[conv](conversation://1234) [anchor](#overview)\n"
                "Ignored placeholders: [tpl](docs/<topic>.md) [ angle ](<id>) [var]({dir}/spec.md)\n"
                "Ignored code span: `[example](does-not-exist.md)`\n"
                "```markdown\n"
                "[fenced-example](also-missing.md)\n"
                "```\n"
                "Broken relative: [`missing.md`](references/missing.md)\n"
                f"Broken file URI outside skill: [gone]({(self.root / 'outside' / 'gone.md').as_uri()})\n"
            ),
        )
        write_text(skill_dir / "references" / "api.md", "# API\n")

        issues = quality.inspect_copy(copy)
        broken = [i for i in issues if i.code == "broken-link"]
        self.assertEqual(len(broken), 2)
        details = " | ".join(i.detail for i in broken)
        self.assertIn("references/missing.md", details)
        self.assertIn("gone.md", details)
        self.assertNotIn(str(self.root), details)

    # ------------------------------------------------------------------
    # Unreferenced vs referenced support files & metadata exclusions

    def test_unreferenced_vs_referenced_support_files_and_metadata_exclusion(self):
        skill_dir, copy = self.make_skill(
            "toolkit",
            (
                "---\n"
                "name: toolkit\n"
                "description: Run helper scripts and consult reference templates when packaging releases.\n"
                "---\n"
                "Run `python3 scripts/build.py` and check [schema](references/schema.json).\n"
                "All templates in `templates/` are loaded automatically.\n"
                "Also inspect `helper.sh` before publishing.\n"
            ),
        )
        # Referenced support files
        write_text(skill_dir / "scripts" / "build.py", "print('ok')\n")
        write_text(skill_dir / "references" / "schema.json", "{}\n")
        write_text(skill_dir / "templates" / "base.html", "<html/>\n")
        write_text(skill_dir / "helper.sh", "#!/bin/sh\n")

        # Excluded metadata / packaging / hidden / skipped dirs
        for meta in (
            ".hidden",
            "agents/openai.yaml",
            "LICENSE",
            "README.md",
            "CHANGELOG.md",
            "package.json",
            "package-lock.json",
            "pyproject.toml",
            "plugin.json",
            "manifest.json",
            "scripts/__init__.py",
            "__pycache__/build.cpython-312.pyc",
            "node_modules/pkg/index.js",
        ):
            write_text(skill_dir / meta, "meta\n")

        # Unreferenced support files (note: `references/schema.json` must NOT mask `references/orphan.md`!)
        write_text(skill_dir / "references" / "orphan.md", "unused\n")
        write_text(skill_dir / "scripts" / "dead_code.py", "unused\n")

        issues = quality.inspect_copy(copy)
        unref = [i.detail for i in issues if i.code == "unreferenced-file"]
        self.assertEqual(
            unref,
            [
                "unreferenced support file: references/orphan.md",
                "unreferenced support file: scripts/dead_code.py",
            ],
        )

    # ------------------------------------------------------------------
    # Aggregation, privacy, render, quality_map & CSV export

    def test_aggregation_privacy_render_quality_map_and_csv(self):
        clean_dir, clean_copy = self.make_skill(
            "Clean-Skill",
            "---\nname: Clean-Skill\ndescription: Use when formatting clean architectural decision records.\n---\n# Clean\n",
            source="personal",
            agents=("claude",),
        )
        # Second copy of same skill (case-insensitive grouping)
        _, clean_copy_2 = self.make_skill(
            "clean-skill",
            "---\nname: clean-skill\ndescription: Use when formatting clean architectural decision records.\n---\n# Clean\n",
            source="project",
            agents=("codex",),
            subdir="workspace-skills",
        )

        info_dir, info_copy = self.make_skill(
            "info-skill",
            "---\nname: info-skill\ndescription: Generate release changelogs from commit history when tagging.\n---\n# Info\n",
            source="personal",
        )
        write_text(info_dir / "scripts" / "unused.py", "pass\n")

        warn_dir, warn_copy = self.make_skill(
            "warn-skill",
            "---\nname: warn-skill\ndescription: Too short.\n---\nSee [missing](refs/missing.md).\n",
            source="plugin:demo/pkg",
        )

        copies = [clean_copy, clean_copy_2, info_copy, warn_copy]
        summary = quality.audit(copies)
        self.assertEqual(summary.total_skills, 3)
        self.assertEqual(summary.clean_skills, 1)
        self.assertEqual(summary.info_only_skills, 1)
        self.assertEqual(summary.warning_skills, 1)
        self.assertEqual(summary.counts_by_code, {"broken-link": 1, "unreferenced-file": 1, "weak-description": 1})

        # Privacy check on render() and render(issues_only=True)
        full_md = quality.render(summary, issues_only=False)
        issues_md = quality.render(summary, issues_only=True)
        self.assertIn("# Skill quality audit report", full_md)
        self.assertIn("## Clean skills", full_md)
        self.assertIn("- Clean-Skill (personal, project)", full_md)
        self.assertNotIn("## Clean skills", issues_md)
        self.assertIn("warn-skill", issues_md)
        self.assertIn("info-skill", issues_md)
        self.assertNotIn(str(self.root), full_md)
        self.assertNotIn(self.root.name, full_md)

        # Privacy & case-insensitive lookup on quality_map()
        qmap = quality.quality_map(copies)
        serialized = json.dumps(qmap)
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn(self.root.name, serialized)
        self.assertNotIn("SKILL.md", serialized)
        self.assertTrue(qmap["Clean-Skill"]["ok"])
        self.assertTrue(qmap["clean-skill"]["ok"])
        self.assertEqual(qmap.get("WARN-SKILL")["warningCount"], 2)
        self.assertFalse(qmap["warn-skill"]["ok"])
        self.assertTrue(qmap["info-skill"]["ok"])
        self.assertEqual(qmap["info-skill"]["infoCount"], 1)

        # CSV export check
        csv_path = self.root / "audit.csv"
        quality.write_csv(summary, csv_path)
        with open(csv_path, newline="", encoding="utf-8") as fh:
            rows = {r["skill"]: r for r in csv.DictReader(fh)}
        self.assertEqual(rows["warn-skill"]["status"], "warning")
        self.assertEqual(rows["warn-skill"]["warnings"], "2")
        self.assertEqual(rows["info-skill"]["status"], "info")
        self.assertEqual(rows["info-skill"]["infos"], "1")
        self.assertEqual(rows["Clean-Skill"]["status"], "clean")
        self.assertEqual(rows["Clean-Skill"]["copies"], "2")
        self.assertEqual(rows["Clean-Skill"]["sources"], "personal;project")
        self.assertIn(str(clean_dir / "SKILL.md"), rows["Clean-Skill"]["paths"])

    def test_edge_cases_bom_crlf_dir_links_dedupe_empty_and_scan_cap(self):
        # Empty inventory
        empty = quality.audit([])
        self.assertEqual(empty.total_skills, 0)
        self.assertIn("No skills found in inventory.", quality.render(empty))

        # UTF-8 BOM + CRLF + `>` and `|-` scalars + directory link `[refs](references/)`
        skill_dir = self.root / "crlf" / "bom-skill"
        skill_dir.mkdir(parents=True, exist_ok=True)
        raw_bytes = (
            "\ufeff---\r\n"
            "name: bom-skill\r\n"
            "summary: |-\r\n"
            "  Compact helper\r\n"
            "description: >\r\n"
            "  Use this skill when inspecting directory links\r\n"
            "  and verifying CRLF YAML parsing.\r\n"
            "---\r\n"
            "Browse [reference docs](references/) before running.\r\n"
        ).encode("utf-8")
        (skill_dir / "SKILL.md").write_bytes(raw_bytes)
        write_text(skill_dir / "references" / "deep" / "spec.md", "# Spec\n")
        bom_copy = SkillCopy("bom-skill", str(skill_dir / "SKILL.md"), "personal")
        self.assertEqual(quality.inspect_copy(bom_copy), [])

        # Duplicate copies with identical issue deduplicate `(code, detail)`
        _, dup1 = self.make_skill("dup-warn", "# Missing frontmatter\n", source="personal", subdir="d1")
        _, dup2 = self.make_skill("dup-warn", "# Missing frontmatter\n", source="project", subdir="d2")
        dup_summary = quality.audit([dup1, dup2])
        self.assertEqual(dup_summary.rows[0].warning_count, 1)

        # Support walk caps at MAX_SUPPORT_SCAN (200) files
        many_dir, many_copy = self.make_skill(
            "many-files",
            "---\nname: many-files\ndescription: Use when stress-testing support file directory walks.\n---\n# Many\n",
        )
        for idx in range(215):
            write_text(many_dir / "assets" / f"item_{idx:03d}.txt", "x")
        unref_issues = [i for i in quality.inspect_copy(many_copy) if i.code == "unreferenced-file"]
        self.assertLessEqual(len(unref_issues), quality.MAX_SUPPORT_SCAN)
        self.assertGreater(len(unref_issues), 190)


if __name__ == "__main__":
    unittest.main()
