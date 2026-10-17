from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

AGENTS = ("copilot", "claude", "antigravity", "pi", "hermes")
ALL_AGENTS = AGENTS
PERSONAL_DIRS = (".copilot/skills", ".claude/skills", ".agents/skills")
PROJECT_DIRS = (".github/skills", ".claude/skills", ".agents/skills")
PLUGIN_STORES = (".copilot/installed-plugins", ".vscode/agent-plugins")
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv"}
SUPPORT_DIRS = {"references", "templates", "assets", "scripts", "examples", "resources"}

_PERSONAL_AGENT_DIRS = (
    (".copilot/skills", ("copilot",), False),
    (".claude/skills", ("copilot", "claude"), False),
    (".agents/skills", AGENTS, False),
    (".gemini/config/skills", ("antigravity",), True),
    (".pi/agent/skills", ("pi",), True),
)

_PROJECT_AGENT_DIRS = (
    (".github/skills", ("copilot",)),
    (".claude/skills", ("copilot", "claude")),
    (".agents/skills", AGENTS),
    (".agent/skills", ("antigravity",)),
    (".pi/skills", ("pi",)),
    (".hermes/skills", ("hermes",)),
)

_ANTIGRAVITY_BUILTIN_DIRS = (
    ".gemini/antigravity/builtin/skills",
    ".gemini/antigravity-ide/builtin/skills",
)


@dataclass(frozen=True)
class SkillCopy:
    name: str
    path: str
    source: str
    agents: tuple[str, ...] = ("copilot",)


def _is_dir(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


def _is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


def _iterdir(path: Path) -> list[Path]:
    try:
        return sorted(path.iterdir())
    except OSError:
        return []


def _flat(root: Path, source: str, agents: tuple[str, ...] = ("copilot",)) -> list[SkillCopy]:
    copies: list[SkillCopy] = []
    if not _is_dir(root):
        return copies
    for entry in _iterdir(root):
        skill_file = entry / "SKILL.md"
        if _is_dir(entry) and _is_file(skill_file):
            copies.append(SkillCopy(entry.name, os.path.normpath(str(skill_file)), source, agents))
    return copies


def _walk_skills(root: Path, source: str, agents: tuple[str, ...]) -> list[SkillCopy]:
    copies: list[SkillCopy] = []
    if not _is_dir(root):
        return copies
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        if "SKILL.md" in filenames and Path(dirpath) != root:
            path = os.path.normpath(os.path.join(dirpath, "SKILL.md"))
            copies.append(SkillCopy(Path(dirpath).name, path, source, agents))
            dirnames[:] = [d for d in dirnames if d not in SUPPORT_DIRS]
    return copies


def _plugin_store(store: Path, agents: tuple[str, ...] = ("copilot",)) -> list[SkillCopy]:
    copies: list[SkillCopy] = []
    if not _is_dir(store):
        return copies
    for dirpath, dirnames, filenames in os.walk(store):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        if "SKILL.md" not in filenames:
            continue
        rel = Path(dirpath).relative_to(store).parts
        if len(rel) < 2 or rel[0].startswith("."):
            continue
        path = os.path.normpath(os.path.join(dirpath, "SKILL.md"))
        copies.append(SkillCopy(Path(dirpath).name, path, f"plugin:{rel[0]}/{rel[1]}", agents))
        dirnames[:] = []
    return copies


def _named_plugin_store(store: Path, prefix: str, agents: tuple[str, ...]) -> list[SkillCopy]:
    copies: list[SkillCopy] = []
    if not _is_dir(store):
        return copies
    for plugin_dir in _iterdir(store):
        if not _is_dir(plugin_dir) or plugin_dir.name.startswith("."):
            continue
        skills_root = plugin_dir / "skills"
        if _is_dir(skills_root):
            copies.extend(_walk_skills(skills_root, f"plugin:{prefix}/{plugin_dir.name}", agents))
    return copies


def _pi_package_store(pi_agent_dir: Path) -> list[SkillCopy]:
    copies: list[SkillCopy] = []
    git_store = pi_agent_dir / "git"
    if _is_dir(git_store):
        for dirpath, dirnames, filenames in os.walk(git_store):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
            if "SKILL.md" not in filenames:
                continue
            rel = Path(dirpath).relative_to(git_store).parts
            if len(rel) < 4:
                continue
            pkg_label = f"{rel[1]}/{rel[2]}" if len(rel) >= 3 else rel[0]
            path = os.path.normpath(os.path.join(dirpath, "SKILL.md"))
            copies.append(SkillCopy(Path(dirpath).name, path, f"plugin:pi/{pkg_label}", ("pi",)))
            dirnames[:] = []
    npm_store = pi_agent_dir / "npm"
    if _is_dir(npm_store):
        for pkg_dir in _iterdir(npm_store):
            if not _is_dir(pkg_dir) or pkg_dir.name.startswith("."):
                continue
            skills_root = pkg_dir / "skills"
            if _is_dir(skills_root):
                copies.extend(_walk_skills(skills_root, f"plugin:pi/{pkg_dir.name}", ("pi",)))
    return copies


def _claude_plugin_store(plugins_root: Path) -> list[SkillCopy]:
    copies: list[SkillCopy] = []
    if not _is_dir(plugins_root):
        return copies
    import json

    manifest = plugins_root / "installed_plugins.json"
    if _is_file(manifest):
        try:
            data = json.loads(manifest.read_text(encoding="utf-8-sig", errors="replace"))
        except (OSError, ValueError):
            data = {}
        plugins_map = data.get("plugins") if isinstance(data, dict) else None
        if isinstance(plugins_map, dict):
            for key, entries in sorted(plugins_map.items()):
                if not isinstance(key, str) or not isinstance(entries, list):
                    continue
                plugin_name = key.split("@", 1)[0].strip() or key
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    install_path = entry.get("installPath")
                    if isinstance(install_path, str) and install_path:
                        skills_root = Path(install_path) / "skills"
                        if _is_dir(skills_root):
                            copies.extend(_walk_skills(skills_root, f"plugin:claude/{plugin_name}", ("claude",)))

    cache_root = plugins_root / "cache"
    if _is_dir(cache_root):
        for market_dir in _iterdir(cache_root):
            if not _is_dir(market_dir) or market_dir.name.startswith("."):
                continue
            for plugin_dir in _iterdir(market_dir):
                if not _is_dir(plugin_dir) or plugin_dir.name.startswith("."):
                    continue
                for ver_dir in _iterdir(plugin_dir):
                    if not _is_dir(ver_dir) or ver_dir.name.startswith("."):
                        continue
                    skills_root = ver_dir / "skills"
                    if _is_dir(skills_root):
                        copies.extend(_walk_skills(skills_root, f"plugin:claude/{plugin_dir.name}", ("claude",)))

    reserved = {"cache", "marketplaces", "data"}
    for entry in _iterdir(plugins_root):
        if not _is_dir(entry) or entry.name.startswith(".") or entry.name in reserved:
            continue
        skills_root = entry / "skills"
        if _is_dir(skills_root):
            copies.extend(_walk_skills(skills_root, f"plugin:claude/{entry.name}", ("claude",)))
    return copies


def _hermes_skills(root: Path) -> list[SkillCopy]:
    copies: list[SkillCopy] = []
    if not _is_dir(root):
        return copies
    bundled: set[str] = set()
    manifest = root / ".bundled_manifest"
    if _is_file(manifest):
        try:
            for line in manifest.read_text(encoding="utf-8-sig", errors="replace").splitlines():
                name = line.split(":", 1)[0].strip()
                if name:
                    bundled.add(name)
        except OSError:
            pass
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        if "SKILL.md" not in filenames or Path(dirpath) == root:
            continue
        skill_name = Path(dirpath).name
        source = "builtin:hermes" if skill_name in bundled else "personal"
        path = os.path.normpath(os.path.join(dirpath, "SKILL.md"))
        copies.append(SkillCopy(skill_name, path, source, ("hermes",)))
        dirnames[:] = []
    return copies


def scan(home: Path, workspaces: list[Path], agent: str | None = None) -> list[SkillCopy]:
    found: dict[str, SkillCopy] = {}

    def add(items):
        for copy in items:
            key = os.path.normcase(copy.path)
            existing = found.get(key)
            if existing is None:
                found[key] = copy
            else:
                merged_agents = tuple(dict.fromkeys((*existing.agents, *copy.agents)))
                if merged_agents != existing.agents:
                    found[key] = SkillCopy(existing.name, existing.path, existing.source, merged_agents)

    for rel, agents, recursive in _PERSONAL_AGENT_DIRS:
        if recursive:
            add(_walk_skills(home / rel, "personal", agents))
        else:
            add(_flat(home / rel, "personal", agents))
    add(_hermes_skills(home / ".hermes/skills"))

    for workspace in workspaces:
        for rel, agents in _PROJECT_AGENT_DIRS:
            add(_flat(workspace / rel, "project", agents))

    for rel in PLUGIN_STORES:
        add(_plugin_store(home / rel, ("copilot",)))
    add(_claude_plugin_store(home / ".claude/plugins"))
    add(_named_plugin_store(home / ".gemini/config/plugins", "antigravity", ("antigravity",)))
    add(_named_plugin_store(home / ".hermes/plugins", "hermes", ("hermes",)))
    add(_pi_package_store(home / ".pi/agent"))

    for rel in _ANTIGRAVITY_BUILTIN_DIRS:
        add(_flat(home / rel, "builtin:antigravity", ("antigravity",)))

    results = sorted(found.values(), key=lambda c: (c.name, c.path))
    if agent and agent != "all":
        results = [c for c in results if agent in c.agents]
    return results


def known_names(home: Path, workspaces: list[Path], agent: str | None = None) -> set[str]:
    return {copy.name for copy in scan(home, workspaces, agent=agent)}
