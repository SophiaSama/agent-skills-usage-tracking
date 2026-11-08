from __future__ import annotations

import os
import re
from datetime import datetime, timezone

READ_TOOL_NAMES = {"read_file", "Read", "view", "readFile", "view_file", "read"}
READ_TOOL_NAMES_LOWER = {t.lower() for t in READ_TOOL_NAMES}
SKILL_TOOL_NAMES = {"Skill", "skill", "skill_view"}
SKILL_TOOL_NAMES_LOWER = {t.lower() for t in SKILL_TOOL_NAMES}
PATH_KEYS = (
    "filePath",
    "file_path",
    "path",
    "AbsolutePath",
    "absolutePath",
    "filepath",
    "absolute_path",
    "file",
)
SKILL_NAME_KEYS = ("skill", "name", "skill_name", "skillName", "SkillName", "skill_id", "skillId")
_EXCLUDED_PARTS = {".archive", ".curator_backups", ".hub"}

_NAME = r"[a-zA-Z0-9][a-zA-Z0-9_-]*"
SLASH_RE = re.compile(rf"^\s*/(?:({_NAME}):)?({_NAME})(?=\s|$)(?:\s+({_NAME})(?=\s|$))?", re.IGNORECASE)
NAME_RE = re.compile(rf"^(?:{_NAME}:)?({_NAME})$", re.IGNORECASE)
_CMD_NAME_RE = re.compile(r"<command-name>\s*([^<]+?)\s*</command-name>", re.IGNORECASE)
_CMD_ARGS_RE = re.compile(r"<command-args>\s*(.*?)\s*</command-args>", re.DOTALL | re.IGNORECASE)


def _clean_str(value):
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if len(cleaned) >= 2 and ((cleaned[0] == '"' and cleaned[-1] == '"') or (cleaned[0] == "'" and cleaned[-1] == "'")):
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
    if name.lower() in ("skills", "references", "templates", "assets", "scripts", "examples", "resources"):
        return None
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
        else:
            tc = event.get("toolCall")
            if isinstance(tc, dict):
                args = tc.get("args")
                if isinstance(args, dict):
                    raw_tc_cwd = _clean_str(args.get("Cwd") or args.get("cwd"))
                    if raw_tc_cwd:
                        cwd = raw_tc_cwd
    cleaned_cwd = _clean_str(cwd) if isinstance(cwd, str) else None
    workspace = os.path.basename(os.path.normpath(cleaned_cwd)) if cleaned_cwd else ""
    session_id = event.get("session_id") or event.get("sessionId") or event.get("conversationId") or ""
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
    for key in (
        "StartLine",
        "start_line",
        "startLine",
        "offset",
        "ContentOffset",
        "content_offset",
        "contentOffset",
    ):
        raw = data.get(key)
        if raw is None:
            continue
        cleaned = _clean_str(raw) if isinstance(raw, str) else raw
        try:
            val = int(cleaned)
            if key.lower().replace("_", "") == "contentoffset":
                if val > 0:
                    return True
            elif val > 1:
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
    tool_lower = tool.lower() if isinstance(tool, str) else ""
    if tool in READ_TOOL_NAMES or tool_lower in READ_TOOL_NAMES_LOWER:
        if _is_paginated_continuation(data):
            return None
        for key in PATH_KEYS:
            found = skill_from_path(data.get(key))
            if found:
                return _record(event, "load", skill=found[0], path=found[1], source="agent-read")
        return None
    if tool in SKILL_TOOL_NAMES or tool_lower in SKILL_TOOL_NAMES_LOWER:
        if (tool == "skill_view" or tool_lower == "skill_view") and any(
            _clean_str(data.get(k)) for k in ("file_path", "filePath", "path", "file")
        ):
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
    name_map = {n.lower(): n for n in names}
    for candidate in (match.group(2), match.group(3), match.group(1)):
        if not candidate:
            continue
        if candidate in names:
            return _record(event, "load", skill=candidate, path="", source="user-slash")
        cand_lower = candidate.lower()
        if cand_lower in name_map:
            return _record(event, "load", skill=name_map[cand_lower], path="", source="user-slash")
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
    if event.get("invocationNum") in (1, "1") and "conversationId" in event:
        return _record(event, "session")
    return None
