from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PERSONAL_DIRS = (".copilot/skills", ".claude/skills", ".agents/skills")
PROJECT_DIRS = (".github/skills", ".claude/skills", ".agents/skills")
PLUGIN_STORES = (".copilot/installed-plugins", ".vscode/agent-plugins")
SKIP_DIRS = {".git", "node_modules", "__pycache__"}


@dataclass(frozen=True)
class SkillCopy:
    name: str
    path: str
    source: str


def _flat(root: Path, source: str) -> list[SkillCopy]:
    copies: list[SkillCopy] = []
    if not root.is_dir():
        return copies
    for entry in sorted(root.iterdir()):
        skill_file = entry / "SKILL.md"
        if entry.is_dir() and skill_file.is_file():
            copies.append(SkillCopy(entry.name, os.path.normpath(str(skill_file)), source))
    return copies


def _plugin_store(store: Path) -> list[SkillCopy]:
    copies: list[SkillCopy] = []
    if not store.is_dir():
        return copies
    for dirpath, dirnames, filenames in os.walk(store):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        if "SKILL.md" not in filenames:
            continue
        rel = Path(dirpath).relative_to(store).parts
        if len(rel) < 2 or rel[0].startswith("."):
            continue
        path = os.path.normpath(os.path.join(dirpath, "SKILL.md"))
        copies.append(SkillCopy(Path(dirpath).name, path, f"plugin:{rel[0]}/{rel[1]}"))
    return copies


def scan(home: Path, workspaces: list[Path]) -> list[SkillCopy]:
    found: dict[str, SkillCopy] = {}

    def add(items):
        for copy in items:
            found.setdefault(os.path.normcase(copy.path), copy)

    for rel in PERSONAL_DIRS:
        add(_flat(home / rel, "personal"))
    for workspace in workspaces:
        for rel in PROJECT_DIRS:
            add(_flat(workspace / rel, "project"))
    for rel in PLUGIN_STORES:
        add(_plugin_store(home / rel))
    return sorted(found.values(), key=lambda c: (c.name, c.path))


def known_names(home: Path, workspaces: list[Path]) -> set[str]:
    return {copy.name for copy in scan(home, workspaces)}
