from __future__ import annotations

import os
import re
from datetime import datetime, timezone

READ_TOOL_NAMES = {"read_file", "Read", "view", "readFile"}
SKILL_TOOL_NAMES = {"Skill", "skill"}
PATH_KEYS = ("filePath", "file_path", "path")
SKILL_NAME_KEYS = ("skill", "name")

_NAME = r"[a-z0-9][a-z0-9-]*"
SLASH_RE = re.compile(rf"^\s*/(?:({_NAME}):)?({_NAME})(?:\s|$)")
NAME_RE = re.compile(rf"^(?:{_NAME}:)?({_NAME})$")


def skill_from_path(value):
    if not isinstance(value, str) or not value:
        return None
    norm = os.path.normpath(value)
    if os.path.basename(norm).lower() != "skill.md":
        return None
    folder = os.path.dirname(norm)
    parts = [p.lower() for p in folder.replace("\\", "/").split("/")]
    if "skills" not in parts:
        return None
    name = os.path.basename(folder)
    return (name, norm) if name else None


def _record(event, type_, **extra):
    ts = event.get("timestamp")
    if not isinstance(ts, str) or not ts:
        ts = datetime.now(timezone.utc).isoformat()
    cwd = event.get("cwd")
    workspace = os.path.basename(os.path.normpath(cwd)) if isinstance(cwd, str) and cwd else ""
    record = {
        "type": type_,
        "ts": ts,
        "session_id": str(event.get("session_id") or ""),
        "workspace": workspace,
    }
    record.update(extra)
    return record


def _from_tool(event):
    tool = event.get("tool_name")
    data = event.get("tool_input")
    if not isinstance(data, dict):
        return None
    if tool in READ_TOOL_NAMES:
        for key in PATH_KEYS:
            found = skill_from_path(data.get(key))
            if found:
                return _record(event, "load", skill=found[0], path=found[1], source="agent-read")
        return None
    if tool in SKILL_TOOL_NAMES:
        for key in SKILL_NAME_KEYS:
            value = data.get(key)
            match = NAME_RE.match(value) if isinstance(value, str) else None
            if match:
                return _record(event, "load", skill=match.group(1), path="", source="skill-tool")
    return None


def _from_prompt(event, known_names):
    prompt = event.get("prompt")
    if not isinstance(prompt, str):
        return None
    match = SLASH_RE.match(prompt)
    if not match:
        return None
    name = match.group(2)
    if name not in known_names():
        return None
    return _record(event, "load", skill=name, path="", source="user-slash")


def detect(event, known_names):
    kind = event.get("hook_event_name")
    if kind == "SessionStart":
        return _record(event, "session")
    if kind == "PostToolUse":
        return _from_tool(event)
    if kind == "UserPromptSubmit":
        return _from_prompt(event, known_names)
    return None
