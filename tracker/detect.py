from __future__ import annotations

import os
import re
from datetime import datetime, timezone

READ_TOOL_NAMES = {"read_file", "Read", "view", "readFile", "view_file", "read", "file_read", "file_view"}
SKILL_TOOL_NAMES = {"Skill", "skill", "skill_view", "skill_tool", "load_skill"}
PATH_KEYS = ("filePath", "file_path", "filepath", "path", "AbsolutePath", "target_file", "TargetFile", "targetFile", "file", "filename", "fileName", "absolute_path", "absolutePath", "relative_path", "relativePath")
SKILL_NAME_KEYS = ("skill", "name", "skill_name", "skillName")
_EXCLUDED_PARTS = {".archive", ".curator_backups", ".hub"}

_NAME = r"[a-zA-Z0-9][a-zA-Z0-9_-]*"
SLASH_RE = re.compile(rf"^\s*/(?:({_NAME}):)?({_NAME})(?=\s|$)(?:\s+({_NAME})(?=\s|$))?", re.IGNORECASE)
NAME_RE = re.compile(rf"^(?:{_NAME}:)?({_NAME})$", re.IGNORECASE)
_CMD_NAME_RE = re.compile(r"<command-name>\s*([^<]+?)\s*</command-name>")
_CMD_ARGS_RE = re.compile(r"<command-args>\s*(.*?)\s*</command-args>", re.DOTALL)
_CODEX_HEADER_RE = re.compile(r"^\s*#{1,3}\s*My request for Codex\s*:?\s*", re.IGNORECASE)


def _epoch_to_iso(value) -> str | None:
    if isinstance(value, (int, float)) and value > 0:
        try:
            f = float(value)
            if f > 1e17:
                f /= 1e9
            elif f > 1e14:
                f /= 1e6
            elif f > 1e11:
                f /= 1e3
            return datetime.fromtimestamp(f, tz=timezone.utc).isoformat()
        except (OSError, OverflowError, ValueError):
            return None
    if isinstance(value, str) and value.strip():
        try:
            f = float(value.strip())
            if f > 0:
                if f > 1e17:
                    f /= 1e9
                elif f > 1e14:
                    f /= 1e6
                elif f > 1e11:
                    f /= 1e3
                return datetime.fromtimestamp(f, tz=timezone.utc).isoformat()
        except (OSError, OverflowError, ValueError):
            pass
        from tracker.report import parse_ts
        parsed = parse_ts(value.strip())
        return parsed.isoformat() if parsed else None
    return None


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
    for path_key in (
        "transcript_path",
        "transcriptPath",
        "log_path",
        "session_path",
        "sessionPath",
        "log_file",
        "session_file",
        "sessionFile",
        "logFile",
    ):
        transcript_path = event.get(path_key)
        if isinstance(transcript_path, str):
            norm = transcript_path.replace("\\", "/")
            parts = set(norm.split("/"))
            if ".claude" in parts or "/.claude/" in norm:
                return "claude"
            if ".codex" in parts or "/.codex/" in norm:
                return "codex"
            if ".pi" in parts or "/.pi/" in norm:
                return "pi"
            if ".hermes" in parts or "/.hermes/" in norm:
                return "hermes"
    tool_input = event.get("tool_input")
    if not isinstance(tool_input, dict):
        for k in ("input", "args", "arguments", "parameters", "params"):
            candidate = event.get(k)
            if isinstance(candidate, dict):
                tool_input = candidate
                break
    if isinstance(tool_input, dict):
        for key in PATH_KEYS:
            val = tool_input.get(key)
            if isinstance(val, str):
                norm = val.replace("\\", "/")
                parts = set(norm.split("/"))
                if ".codex" in parts or "/.codex/" in norm:
                    return "codex"
                if ".claude" in parts or "/.claude/" in norm:
                    return "claude"
                if ".pi" in parts or "/.pi/" in norm:
                    return "pi"
                if ".hermes" in parts or "/.hermes/" in norm:
                    return "hermes"
    return None


def _record(event, type_, **extra):
    raw_ts = event.get("timestamp") or event.get("ts")
    ts = None
    if isinstance(raw_ts, str) and raw_ts.strip():
        try:
            f = float(raw_ts.strip())
            ts = _epoch_to_iso(f)
        except ValueError:
            ts = raw_ts.strip()
    elif isinstance(raw_ts, (int, float)):
        ts = _epoch_to_iso(raw_ts)
    if not ts:
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
    for key in ("StartLine", "startLine", "start_line", "line_offset", "lineOffset"):
        raw = data.get(key)
        if raw is None:
            continue
        cleaned = _clean_str(raw) if isinstance(raw, str) else raw
        try:
            if int(cleaned) > 1:
                return True
        except (TypeError, ValueError):
            pass
    for key in ("offset", "ContentOffset", "contentOffset", "content_offset", "byte_offset", "byteOffset"):
        raw = data.get(key)
        if raw is None:
            continue
        cleaned = _clean_str(raw) if isinstance(raw, str) else raw
        try:
            if int(cleaned) > 0:
                return True
        except (TypeError, ValueError):
            pass
    for key in ("page", "page_number", "pageNumber"):
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
    tool = event.get("tool_name") or event.get("tool") or event.get("name")
    data = event.get("tool_input")
    if not isinstance(data, dict):
        for k in ("input", "args", "arguments", "parameters", "params"):
            candidate = event.get(k)
            if isinstance(candidate, dict):
                data = candidate
                break
    if not isinstance(data, dict):
        tool_call = event.get("toolCall") or event.get("tool_call") or event.get("function")
        if isinstance(tool_call, dict):
            tool = tool or tool_call.get("name") or tool_call.get("tool_name") or tool_call.get("tool")
            raw_args = (
                tool_call.get("args")
                or tool_call.get("arguments")
                or tool_call.get("input")
                or tool_call.get("parameters")
                or tool_call.get("params")
            )
            if isinstance(raw_args, dict):
                data = raw_args
    if not isinstance(data, dict):
        return None
    tool_base = (
        tool.split(":")[-1].split("/")[-1].split(".")[-1].split("__")[-1]
        if isinstance(tool, str)
        else ""
    )
    if (
        tool in READ_TOOL_NAMES
        or tool_base in READ_TOOL_NAMES
        or tool_base.lower() in {t.lower() for t in READ_TOOL_NAMES}
    ):
        if _is_paginated_continuation(data):
            return None
        for key in PATH_KEYS:
            found = skill_from_path(data.get(key))
            if found:
                return _record(event, "load", skill=found[0], path=found[1], source="agent-read")
        return None
    if (
        tool in SKILL_TOOL_NAMES
        or tool_base in SKILL_TOOL_NAMES
        or tool_base.lower() in {t.lower() for t in SKILL_TOOL_NAMES}
    ):
        if (tool == "skill_view" or tool_base.lower() == "skill_view") and _clean_str(data.get("file_path")):
            return None
        for key in SKILL_NAME_KEYS:
            value = _clean_str(data.get(key))
            match = NAME_RE.match(value) if isinstance(value, str) else None
            if match:
                return _record(event, "load", skill=match.group(1), path="", source="skill-tool")
    return None


def _normalize_prompt(prompt: str) -> str:
    if not isinstance(prompt, str):
        return ""
    if _CODEX_HEADER_RE.search(prompt):
        prompt = _CODEX_HEADER_RE.sub("", prompt, count=1).strip()
    cmd_match = _CMD_NAME_RE.search(prompt)
    if cmd_match:
        cmd = cmd_match.group(1).strip()
        if not cmd.startswith("/"):
            cmd = "/" + cmd
        args_match = _CMD_ARGS_RE.search(prompt)
        args = args_match.group(1).strip() if args_match else ""
        return f"{cmd} {args}".strip()
    lines = prompt.strip().splitlines()
    if lines and lines[0].strip().startswith("#"):
        rest = "\n".join(lines[1:]).strip()
        if rest.startswith("/"):
            return rest
    return prompt


def _from_prompt(event, known_names):
    prompt = event.get("prompt")
    if not isinstance(prompt, str):
        return None
    match = SLASH_RE.match(_normalize_prompt(prompt))
    if not match:
        return None
    names = known_names()
    names_lower = {n.lower(): n for n in names}
    for name in (match.group(2), match.group(1), match.group(3)):
        if not name:
            continue
        if name in names:
            return _record(event, "load", skill=name, path="", source="user-slash")
        if name.lower() in names_lower:
            return _record(event, "load", skill=names_lower[name.lower()], path="", source="user-slash")
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
