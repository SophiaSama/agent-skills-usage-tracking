import tempfile
import unittest
from pathlib import Path

from tracker import inventory


def make_skill(root: Path, *parts):
    folder = root.joinpath(*parts)
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("---\nname: x\n---\n", encoding="utf-8")


def build_tree(tmp):
    home = Path(tmp) / "home"
    ws = Path(tmp) / "ws"
    make_skill(home, ".copilot", "skills", "graphify")
    make_skill(home, ".claude", "skills", "graphify")
    make_skill(home, ".agents", "skills", "grill-me")
    make_skill(ws, ".github", "skills", "local-skill")
    make_skill(home, ".copilot", "installed-plugins", "market", "plug", "skills", "brainstorming")
    make_skill(home, ".vscode", "agent-plugins", "github.com", "owner", "repo", "skills", "engineering", "tdd")
    make_skill(home, ".copilot", "installed-plugins", ".github", "skills", "hidden")
    return home, ws


class InventoryTests(unittest.TestCase):
    def test_scan_tags_each_skill_copy_with_its_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, ws = build_tree(tmp)
            sources = {}
            for copy in inventory.scan(home, [ws]):
                sources.setdefault(copy.name, []).append(copy.source)
            self.assertEqual(sources["graphify"], ["personal", "personal"])
            self.assertEqual(sources["grill-me"], ["personal"])
            self.assertEqual(sources["local-skill"], ["project"])
            self.assertEqual(sources["brainstorming"], ["plugin:market/plug"])
            self.assertEqual(sources["tdd"], ["plugin:github.com/owner"])
            self.assertNotIn("hidden", sources)

    def test_listing_a_workspace_twice_does_not_duplicate_copies(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, ws = build_tree(tmp)
            self.assertEqual(len(inventory.scan(home, [ws, ws])), len(inventory.scan(home, [ws])))

    def test_known_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, ws = build_tree(tmp)
            self.assertEqual(
                inventory.known_names(home, [ws]),
                {"graphify", "grill-me", "local-skill", "brainstorming", "tdd"},
            )

    def test_missing_roots_are_fine(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(inventory.scan(Path(tmp) / "nohome", [Path(tmp) / "nows"]), [])

    def test_walk_skills_detects_lowercase_skill_md(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "workspace"
            skill_dir = ws / "my-lowercase-skill"
            skill_dir.mkdir(parents=True)
            (skill_dir / "skill.md").write_text("# Skill\n", encoding="utf-8")

            copies = inventory._walk_skills(ws, "project", ("copilot",))
            matched = [c for c in copies if c.name == "my-lowercase-skill"]
            self.assertEqual(len(matched), 1)
            self.assertEqual(matched[0].source, "project")


if __name__ == "__main__":
    unittest.main()
