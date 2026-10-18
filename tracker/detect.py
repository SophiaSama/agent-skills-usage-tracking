from __future__ import annotations

import os
import re
from datetime import datetime, timezone

READ_TOOL_NAMES = {"read_file", "Read", "view", "readFile", "view_file", "read"}
SKILL_TOOL_NAMES = {"Skill", "skill", "skill_view"}
PATH_KEYS = ("filePath", "file_path", "path", "AbsolutePath")
SKILL_NAME_KEYS = ("skill", "name")
_EXCLUDED_PARTS = {".archive", ".curator_backups", ".hub"}

_NAME = r"[a-z0-9][a-z0-9_-]*"
SLASH_RE = re.compile(rf"^\s*/(?:({_NAME}):)?({_NAME})(?=\s|$)(?:\s+({_NAME})(?=\s|$))?")
NAME_RE = re.compile(rf"^(?:{_NAME}:)?({_NAME})$")
_CMD_NAME_RE = re.compile(r"<command-name>\s*([^<]+?)\s*</command-name>")
_CMD_ARGS_RE = re.compile(r"<command-args>\s*(.*?)\s*</command-args>", re.DOTALL)


def _clean_str(value):
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if len(cleaned) >= 2 and cleaned[0] == '"' and cleaned[-1] == '"':
        cleaned = cleaned[1:-1]
    return cleaned or None


def skill_from_path(value):
    cleaned = _clean_str(value)
    if not cleaned:
        return None
    raw_parts = [p for p in cleaned.replace("\\", "/").split("/") if p]
    if len(raw_parts) < 2 or raw_parts[-1].lower() != "skill.md":
        return None
    folder_parts = [p.lower() for p in raw_parts[:-1]]
    if "skills" not in folder_parts or any(p in _EXCLUDED_PARTS for p in folder_parts):
        return None
    name = raw_parts[-2]
    return (name, os.path.normpath(cleaned)) if name else None


def _infer_agent(event):
    agent = event.get("agent")
    if isinstance(agent, str) and agent:
        return agent
    if "conversationId" in event:
        return "antigravity"
    transcript_path = event.get("transcript_path")
    if isinstance(transcript_path, str) and ".claude" in transcript_path.replace("\\", "/"):
        return "claude"
    return None


def _record(event, type_, **extra):
    ts = event.get("timestamp")
    if not isinstance(ts, str) or not ts:
        ts = datetime.now(timezone.utc).isoformat()
    cwd = event.get("cwd")
    if not (isinstance(cwd, str) and cwd):
        ws_paths = event.get("workspacePaths")
        if isinstance(ws_paths, list) and ws_paths and isinstance(ws_paths[0], str):
            cwd = ws_paths[0]
    cleaned_cwd = _clean_str(cwd) if isinstance(cwd, str) else None
    workspace = os.path.basename(os.path.normpath(cleaned_cwd)) if cleaned_cwd else ""
    session_id = event.get("session_id") or event.get("conversationId") or ""
    record = {
        "type": type_,
        "ts": ts,
        "session_id": str(session_id),
        "workspace": workspace,
    }
    agent = _infer_agent(event)
    if isinstance(agent, str) and agent:
        record["agent"] = agent
    record.update(extra)
    return record


def _is_paginated_continuation(data: dict) -> bool:
    for key in ("StartLine", "startLine", "start_line", "offset", "Offset"):
        raw = data.get(key)
        if raw is None:
            continue
        cleaned = _clean_str(raw) if isinstance(raw, str) else raw
        try:
            if int(cleaned) > 1:
                return True
        except (TypeError, ValueError):
            pass
    return False


def _from_tool(event):
    tool = event.get("tool_name")
    data = event.get("tool_input")
    if not isinstance(data, dict):
        tool_call = event.get("toolCall")
        if isinstance(tool_call, dict):
            tool = tool_call.get("name")
            data = tool_call.get("args")
    if not isinstance(data, dict):
        return None
    if tool in READ_TOOL_NAMES:
        if _is_paginated_continuation(data):
            return None
        for key in PATH_KEYS:
            found = skill_from_path(data.get(key))
            if found:
                return _record(event, "load", skill=found[0], path=found[1], source="agent-read")
        return None
    if tool in SKILL_TOOL_NAMES:
        if tool == "skill_view" and _clean_str(data.get("file_path")):
            return None
        for key in SKILL_NAME_KEYS:
            value = _clean_str(data.get(key))
            match = NAME_RE.match(value) if isinstance(value, str) else None
            if match:
                return _record(event, "load", skill=match.group(1), path="", source="skill-tool")
    return None


def _normalize_prompt(prompt: str) -> str:
    cmd_match = _CMD_NAME_RE.search(prompt)
    if cmd_match:
        cmd = cmd_match.group(1).strip()
        if not cmd.startswith("/"):
            cmd = "/" + cmd
        args_match = _CMD_ARGS_RE.search(prompt)
        args = args_match.group(1).strip() if args_match else ""
        return f"{cmd} {args}".strip()
    return prompt


def _from_prompt(event, known_names):
    prompt = event.get("prompt")
    if not isinstance(prompt, str):
        return None
    match = SLASH_RE.match(_normalize_prompt(prompt))
    if not match:
        return None
    names = known_names()
    for name in (match.group(2), match.group(3)):
        if name and name in names:
            return _record(event, "load", skill=name, path="", source="user-slash")
    return None


def detect(event, known_names):
    kind = event.get("hook_event_name")
    if kind == "SessionStart":
        return _record(event, "session")
    if kind in ("PostToolUse", "PreToolUse"):
        return _from_tool(event)
    if kind == "UserPromptSubmit":
        return _from_prompt(event, known_names)
    if "toolCall" in event:
        return _from_tool(event)
    if event.get("invocationNum") == 1 and "conversationId" in event:
        return _record(event, "session")
    return None
