from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from tracker import detect, inventory, store
from tracker.report import parse_ts

SYNC_AGENTS = ("claude", "antigravity", "pi", "hermes", "codex")
_USER_REQ_RE = re.compile(
    r"<\s*(?:USER_REQUEST|USER_PROMPT|USER_MESSAGE)(?:\s+[^>]*)?>\s*(.*?)\s*<\s*/(?:USER_REQUEST|USER_PROMPT|USER_MESSAGE)\s*>",
    re.DOTALL | re.IGNORECASE,
)
_PI_SKILL_CMD_RE = re.compile(rf"^\s*/skill:({detect._NAME})(?=\s|$)", re.IGNORECASE)
_HERMES_INVOKE_RE = re.compile(r'^\[IMPORTANT: The user has invoked the "([^"]+)"')
_DEDUP_READ_SECONDS = 60
_LIVE_HOOK_SLACK_SECONDS = 30


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
        parsed = parse_ts(value.strip())
        return parsed.isoformat() if parsed else None
    return None


def _extract_agy_prompt(content: str) -> str:
    match = _USER_REQ_RE.search(content)
    return match.group(1).strip() if match else content.strip()


def _should_skip_recent_read(recent_reads: dict[tuple[str, str], datetime], session_id: str, skill: str, ts_str: str) -> bool:
    stamp = parse_ts(ts_str)
    if stamp is None:
        return False
    key = (session_id, skill)
    prev = recent_reads.get(key)
    if prev is not None and 0 <= abs((stamp - prev).total_seconds()) <= _DEDUP_READ_SECONDS:
        return True
    recent_reads[key] = stamp
    return False


def collect_antigravity_events(home: Path, known_names: set[str]) -> list[dict]:
    events: list[dict] = []
    products = ("antigravity", "antigravity-ide", "antigravity-cli")
    for product in products:
        brain_root = home / ".gemini" / product / "brain"
        if not inventory._is_dir(brain_root):
            continue
        for conv_dir in inventory._iterdir(brain_root):
            if not inventory._is_dir(conv_dir) or conv_dir.name.startswith("."):
                continue
            transcript = conv_dir / ".system_generated" / "logs" / "transcript.jsonl"
            if not inventory._is_file(transcript):
                alt1 = conv_dir / "transcript.jsonl"
                alt2 = conv_dir / "logs" / "transcript.jsonl"
                if inventory._is_file(alt1):
                    transcript = alt1
                elif inventory._is_file(alt2):
                    transcript = alt2
                else:
                    continue
            session_id = conv_dir.name
            first_ts: str | None = None
            cwd: str = ""
            session_loads: list[dict] = []
            recent_reads: dict[tuple[str, str], datetime] = {}
            try:
                with open(transcript, encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        try:
                            step = json.loads(line)
                        except ValueError:
                            continue
                        if not isinstance(step, dict):
                            continue
                        step_ts = _epoch_to_iso(step.get("created_at") or step.get("timestamp"))
                        if step_ts and first_ts is None:
                            first_ts = step_ts
                        step_type = str(step.get("type") or "")
                        if step_type in ("USER_INPUT", "USER", "user", "USER_MESSAGE") or (
                            isinstance(step.get("content"), str) and "/" in step["content"]
                        ):
                            content = step.get("content")
                            if isinstance(content, str) and "/" in content:
                                prompt = _extract_agy_prompt(content)
                                syn = {
                                    "hook_event_name": "UserPromptSubmit",
                                    "timestamp": step_ts,
                                    "session_id": session_id,
                                    "cwd": cwd,
                                    "prompt": prompt,
                                    "agent": "antigravity",
                                }
                                rec = detect.detect(syn, lambda: known_names)
                                if rec:
                                    session_loads.append(rec)
                        tcs = step.get("tool_calls") or step.get("toolCalls") or []
                        if tcs or step_type in ("PLANNER_RESPONSE", "AGENT_RESPONSE", "MODEL_RESPONSE", "TOOL_EXECUTION"):
                            for tc in tcs:
                                if not isinstance(tc, dict):
                                    continue
                                args = tc.get("args")
                                if isinstance(args, str):
                                    try:
                                        args = json.loads(args)
                                        tc["args"] = args
                                    except ValueError:
                                        args = {}
                                if isinstance(args, dict) and not cwd:
                                    raw_cwd = detect._clean_str(args.get("Cwd") or args.get("cwd"))
                                    if raw_cwd:
                                        cwd = raw_cwd
                                syn = {
                                    "timestamp": step_ts,
                                    "session_id": session_id,
                                    "cwd": cwd,
                                    "agent": "antigravity",
                                    "toolCall": tc,
                                }
                                rec = detect.detect(syn, lambda: known_names)
                                if rec:
                                    if rec.get("source") == "agent-read" and _should_skip_recent_read(
                                        recent_reads, session_id, rec["skill"], rec["ts"]
                                    ):
                                        continue
                                    session_loads.append(rec)
            except OSError:
                continue

            if first_ts:
                workspace = os.path.basename(os.path.normpath(cwd)) if cwd else ""
                events.append(
                    {
                        "type": "session",
                        "ts": first_ts,
                        "session_id": session_id,
                        "workspace": workspace,
                        "agent": "antigravity",
                    }
                )
                for load_rec in session_loads:
                    if not load_rec.get("workspace") and workspace:
                        load_rec["workspace"] = workspace
                    events.append(load_rec)
    return events


def collect_pi_events(home: Path, known_names: set[str]) -> list[dict]:
    events: list[dict] = []
    sessions_root = home / ".pi" / "agent" / "sessions"
    if not inventory._is_dir(sessions_root):
        return events
    try:
        session_files = sorted(sessions_root.rglob("*.jsonl"))
    except OSError:
        return events
    for path in session_files:
        session_id = path.stem
        cwd = ""
        session_added = False
        recent_reads: dict[tuple[str, str], datetime] = {}
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(entry, dict):
                        continue
                    etype = entry.get("type")
                    ts = _epoch_to_iso(entry.get("timestamp"))
                    if etype == "session":
                        session_id = str(entry.get("id") or session_id)
                        cwd = str(entry.get("cwd") or cwd)
                        if ts:
                            workspace = os.path.basename(os.path.normpath(cwd)) if cwd else ""
                            events.append(
                                {
                                    "type": "session",
                                    "ts": ts,
                                    "session_id": session_id,
                                    "workspace": workspace,
                                    "agent": "pi",
                                }
                            )
                            session_added = True
                    elif etype == "message":
                        msg = entry.get("message")
                        if not isinstance(msg, dict):
                            continue
                        ts = ts or _epoch_to_iso(msg.get("timestamp"))
                        if not ts:
                            continue
                        if not session_added:
                            workspace = os.path.basename(os.path.normpath(cwd)) if cwd else ""
                            events.append(
                                {
                                    "type": "session",
                                    "ts": ts,
                                    "session_id": session_id,
                                    "workspace": workspace,
                                    "agent": "pi",
                                }
                            )
                            session_added = True
                        role = msg.get("role")
                        content = msg.get("content")
                        if role == "user":
                            texts: list[str] = []
                            if isinstance(content, str):
                                texts.append(content)
                            elif isinstance(content, list):
                                for part in content:
                                    if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                                        texts.append(part["text"])
                            name_map = {n.lower(): n for n in known_names}
                            for text in texts:
                                m = _PI_SKILL_CMD_RE.match(text)
                                if m:
                                    workspace = os.path.basename(os.path.normpath(cwd)) if cwd else ""
                                    skill_name = name_map.get(m.group(1).lower(), m.group(1))
                                    events.append(
                                        {
                                            "type": "load",
                                            "ts": ts,
                                            "session_id": session_id,
                                            "workspace": workspace,
                                            "agent": "pi",
                                            "skill": skill_name,
                                            "path": "",
                                            "source": "user-slash",
                                        }
                                    )
                                    continue
                                syn = {
                                    "hook_event_name": "UserPromptSubmit",
                                    "timestamp": ts,
                                    "session_id": session_id,
                                    "cwd": cwd,
                                    "prompt": text,
                                    "agent": "pi",
                                }
                                rec = detect.detect(syn, lambda: known_names)
                                if rec:
                                    events.append(rec)
                        elif role == "assistant" and isinstance(content, list):
                            for part in content:
                                if not isinstance(part, dict) or part.get("type") != "toolCall":
                                    continue
                                raw_input = part.get("arguments") if part.get("arguments") is not None else part.get("input")
                                if isinstance(raw_input, str):
                                    try:
                                        raw_input = json.loads(raw_input)
                                    except ValueError:
                                        raw_input = {}
                                syn = {
                                    "hook_event_name": "PostToolUse",
                                    "timestamp": ts,
                                    "session_id": session_id,
                                    "cwd": cwd,
                                    "agent": "pi",
                                    "tool_name": part.get("name"),
                                    "tool_input": raw_input if isinstance(raw_input, dict) else {},
                                }
                                rec = detect.detect(syn, lambda: known_names)
                                if rec:
                                    if rec.get("source") == "agent-read" and _should_skip_recent_read(
                                        recent_reads, session_id, rec["skill"], rec["ts"]
                                    ):
                                        continue
                                    events.append(rec)
        except OSError:
            continue
    return events


def collect_hermes_events(home: Path, known_names: set[str]) -> list[dict]:
    events: list[dict] = []
    hermes_dir = home / ".hermes"
    if not inventory._is_dir(hermes_dir):
        return events

    db_path = hermes_dir / "state.db"
    db_skill_counts: dict[str, int] = {}
    session_cwds: dict[str, str] = {}

    if inventory._is_file(db_path):
        conn = None
        try:
            try:
                conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30.0)
            except sqlite3.Error:
                conn = sqlite3.connect(str(db_path), timeout=30.0)
            conn.row_factory = sqlite3.Row

            for row in conn.execute("SELECT id, started_at, cwd, source FROM sessions"):
                sid = str(row["id"] or "")
                ts = _epoch_to_iso(row["started_at"])
                cwd = str(row["cwd"] or "")
                session_cwds[sid] = cwd
                workspace = os.path.basename(os.path.normpath(cwd)) if cwd else str(row["source"] or "")
                if sid and ts:
                    events.append(
                        {
                            "type": "session",
                            "ts": ts,
                            "session_id": sid,
                            "workspace": workspace,
                            "agent": "hermes",
                        }
                    )

            recent_reads: dict[tuple[str, str], datetime] = {}
            name_map = {n.lower(): n for n in known_names}
            try:
                msg_cursor = conn.execute(
                    "SELECT session_id, role, content, tool_calls, timestamp "
                    "FROM messages WHERE COALESCE(active, 1) = 1 ORDER BY timestamp, id"
                )
            except sqlite3.OperationalError:
                try:
                    msg_cursor = conn.execute(
                        "SELECT session_id, role, content, tool_calls, timestamp "
                        "FROM messages ORDER BY timestamp"
                    )
                except sqlite3.OperationalError:
                    msg_cursor = []
            for row in msg_cursor:
                sid = str(row["session_id"] or "")
                ts = _epoch_to_iso(row["timestamp"])
                if not sid or not ts:
                    continue
                cwd = session_cwds.get(sid, "")
                workspace = os.path.basename(os.path.normpath(cwd)) if cwd else ""
                role = row["role"]
                content = row["content"]
                tool_calls_raw = row["tool_calls"]

                if role == "user" and isinstance(content, str) and content:
                    m = _HERMES_INVOKE_RE.match(content)
                    if m:
                        raw_skill = m.group(1).strip().lstrip("/")
                        name_match = detect.NAME_RE.match(raw_skill)
                        raw_name = name_match.group(1) if name_match else raw_skill
                        skill_name = name_map.get(raw_name.lower(), raw_name)
                        if skill_name:
                            events.append(
                                {
                                    "type": "load",
                                    "ts": ts,
                                    "session_id": sid,
                                    "workspace": workspace,
                                    "agent": "hermes",
                                    "skill": skill_name,
                                    "path": "",
                                    "source": "user-slash",
                                }
                            )
                            db_skill_counts[skill_name] = db_skill_counts.get(skill_name, 0) + 1
                    elif content.lstrip().startswith("/"):
                        syn = {
                            "hook_event_name": "UserPromptSubmit",
                            "timestamp": ts,
                            "session_id": sid,
                            "cwd": cwd,
                            "prompt": content,
                            "agent": "hermes",
                        }
                        rec = detect.detect(syn, lambda: known_names)
                        if rec:
                            events.append(rec)
                            db_skill_counts[rec["skill"]] = db_skill_counts.get(rec["skill"], 0) + 1

                if role == "assistant" and tool_calls_raw:
                    try:
                        calls = json.loads(tool_calls_raw) if isinstance(tool_calls_raw, str) else tool_calls_raw
                    except ValueError:
                        calls = []
                    if isinstance(calls, list):
                        for call in calls:
                            if not isinstance(call, dict):
                                continue
                            fn = call.get("function")
                            if not isinstance(fn, dict):
                                continue
                            fn_name = fn.get("name")
                            raw_args = fn.get("arguments")
                            try:
                                args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
                            except ValueError:
                                args = {}
                            if not isinstance(args, dict):
                                continue
                            syn = {
                                "hook_event_name": "PostToolUse",
                                "timestamp": ts,
                                "session_id": sid,
                                "cwd": cwd,
                                "agent": "hermes",
                                "tool_name": fn_name,
                                "tool_input": args,
                            }
                            rec = detect.detect(syn, lambda: known_names)
                            if rec:
                                if rec.get("source") == "agent-read" and _should_skip_recent_read(
                                    recent_reads, sid, rec["skill"], rec["ts"]
                                ):
                                    continue
                                events.append(rec)
                                db_skill_counts[rec["skill"]] = db_skill_counts.get(rec["skill"], 0) + 1
        except sqlite3.Error:
            pass
        finally:
            if conn is not None:
                conn.close()

    usage_file = hermes_dir / "skills" / ".usage.json"
    if inventory._is_file(usage_file):
        try:
            usage_data = json.loads(usage_file.read_text(encoding="utf-8-sig", errors="replace"))
        except (OSError, ValueError):
            usage_data = {}
        if isinstance(usage_data, dict):
            for skill_name, meta in sorted(usage_data.items()):
                if not isinstance(meta, dict):
                    continue
                try:
                    count = max(int(meta.get("use_count") or 0), int(meta.get("view_count") or 0))
                except (ValueError, TypeError):
                    count = 0
                last_raw = meta.get("last_used_at") or meta.get("last_viewed_at")
                last_iso = _epoch_to_iso(last_raw)
                if count > 0 and last_iso and db_skill_counts.get(skill_name, 0) == 0:
                    events.append(
                        {
                            "type": "load",
                            "ts": last_iso,
                            "session_id": f"hermes-usage-{skill_name}",
                            "workspace": "hermes",
                            "agent": "hermes",
                            "skill": skill_name,
                            "path": "",
                            "source": "skill-tool",
                        }
                    )
    return events


def collect_claude_events(home: Path, known_names: set[str]) -> list[dict]:
    events: list[dict] = []
    projects_root = home / ".claude" / "projects"
    if not inventory._is_dir(projects_root):
        return events
    try:
        session_files = sorted(projects_root.rglob("*.jsonl"))
    except OSError:
        return events
    seen_sessions: set[str] = set()
    recent_reads: dict[tuple[str, str], datetime] = {}
    for path in session_files:
        default_sid = path.stem
        cwd = ""
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(entry, dict):
                        continue
                    session_id = str(entry.get("sessionId") or entry.get("session_id") or default_sid)
                    entry_cwd = entry.get("cwd")
                    if isinstance(entry_cwd, str) and entry_cwd:
                        cwd = entry_cwd
                    ts = _epoch_to_iso(entry.get("timestamp"))
                    msg = entry.get("message")
                    if not ts and isinstance(msg, dict):
                        ts = _epoch_to_iso(msg.get("timestamp"))
                    if not ts:
                        continue
                    if session_id and session_id not in seen_sessions:
                        workspace = os.path.basename(os.path.normpath(cwd)) if cwd else ""
                        events.append(
                            {
                                "type": "session",
                                "ts": ts,
                                "session_id": session_id,
                                "workspace": workspace,
                                "agent": "claude",
                            }
                        )
                        seen_sessions.add(session_id)

                    if not isinstance(msg, dict):
                        continue
                    role = msg.get("role") or entry.get("type")
                    content = msg.get("content")
                    if role == "user":
                        texts: list[str] = []
                        if isinstance(content, str):
                            texts.append(content)
                        elif isinstance(content, list):
                            for part in content:
                                if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                                    texts.append(part["text"])
                        for text in texts:
                            if "/" not in text and "<command-name>" not in text.lower():
                                continue
                            syn = {
                                "hook_event_name": "UserPromptSubmit",
                                "timestamp": ts,
                                "session_id": session_id,
                                "cwd": cwd,
                                "prompt": text,
                                "agent": "claude",
                            }
                            rec = detect.detect(syn, lambda: known_names)
                            if rec:
                                events.append(rec)
                    elif role == "assistant" and isinstance(content, list):
                        for part in content:
                            if not isinstance(part, dict) or part.get("type") not in ("tool_use", "toolCall"):
                                continue
                            raw_input = part.get("input") if part.get("input") is not None else part.get("arguments")
                            if isinstance(raw_input, str):
                                try:
                                    raw_input = json.loads(raw_input)
                                except ValueError:
                                    raw_input = {}
                            syn = {
                                "hook_event_name": "PostToolUse",
                                "timestamp": ts,
                                "session_id": session_id,
                                "cwd": cwd,
                                "agent": "claude",
                                "tool_name": part.get("name"),
                                "tool_input": raw_input if isinstance(raw_input, dict) else {},
                            }
                            rec = detect.detect(syn, lambda: known_names)
                            if rec:
                                if rec.get("source") == "agent-read" and _should_skip_recent_read(
                                    recent_reads, session_id, rec["skill"], rec["ts"]
                                ):
                                    continue
                                events.append(rec)
        except OSError:
            continue
    return events


def _extract_codex_tool_call(d: dict) -> tuple[str, dict] | None:
    if not isinstance(d, dict):
        return None
    fn = d.get("function") or d.get("call") or d.get("tool")
    if isinstance(fn, dict):
        name = fn.get("name") or fn.get("tool_name") or d.get("name") or d.get("tool_name") or d.get("tool")
        args = (
            fn.get("arguments")
            or fn.get("args")
            or fn.get("input")
            or fn.get("parameters")
            or fn.get("params")
            or d.get("arguments")
            or d.get("args")
            or d.get("input")
            or d.get("parameters")
            or d.get("params")
        )
    else:
        name = d.get("name") or d.get("tool_name") or d.get("tool")
        args = d.get("arguments") or d.get("args") or d.get("input") or d.get("parameters") or d.get("params")

    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = {}
    return (str(name), args) if name and isinstance(args, dict) else None


def collect_codex_events(home: Path, known_names: set[str]) -> list[dict]:
    events: list[dict] = []
    codex_dir = home / ".codex"
    if not inventory._is_dir(codex_dir):
        return events
    try:
        raw_files = sorted(codex_dir.rglob("*"))
        session_files = [
            p
            for p in raw_files
            if p.is_file()
            and p.suffix in (".jsonl", ".log", ".json")
            and not any(part in ("skills", "history", "plugins", "cache") for part in p.relative_to(codex_dir).parts)
            and p.name not in ("history.jsonl", "session_index.jsonl", "hooks.json", "config.json", "settings.json", ".usage.json")
        ]
    except OSError:
        return events

    seen_sessions: set[str] = set()
    session_events_by_id: dict[str, dict] = {}
    recent_reads: dict[tuple[str, str], datetime] = {}

    for path in session_files:
        default_sid = path.stem
        session_loads: list[dict] = []
        file_top_id = ""
        file_top_cwd = ""
        try:
            entries: list[dict] = []
            with open(path, encoding="utf-8", errors="replace") as fh:
                content = fh.read()
            for line in content.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                    if isinstance(parsed, dict):
                        sub_list = (
                            parsed.get("events")
                            or parsed.get("messages")
                            or parsed.get("items")
                            or parsed.get("steps")
                            or parsed.get("history")
                        )
                        if isinstance(sub_list, list):
                            for item in sub_list:
                                if isinstance(item, dict):
                                    if "session_id" not in item and "id" not in item:
                                        wrap_id = parsed.get("id") or parsed.get("session_id") or parsed.get("sessionId")
                                        if wrap_id:
                                            item["session_id"] = wrap_id
                                    if "cwd" not in item:
                                        wrap_cwd = parsed.get("cwd") or parsed.get("working_directory") or parsed.get("workdir") or parsed.get("workspace")
                                        if wrap_cwd:
                                            item["cwd"] = wrap_cwd
                                    entries.append(item)
                        else:
                            entries.append(parsed)
                except ValueError:
                    continue
            if not entries and content.strip():
                try:
                    full_parsed = json.loads(content)
                    if isinstance(full_parsed, list):
                        entries = [item for item in full_parsed if isinstance(item, dict)]
                    elif isinstance(full_parsed, dict):
                        file_top_id = str(full_parsed.get("id") or full_parsed.get("session_id") or full_parsed.get("sessionId") or "")
                        file_top_cwd = str(full_parsed.get("cwd") or full_parsed.get("working_directory") or full_parsed.get("workdir") or full_parsed.get("workspace") or "")
                        raw_list = (
                            full_parsed.get("events")
                            or full_parsed.get("messages")
                            or full_parsed.get("items")
                            or full_parsed.get("steps")
                            or full_parsed.get("history")
                            or [full_parsed]
                        )
                        if isinstance(raw_list, list):
                            for item in raw_list:
                                if isinstance(item, dict):
                                    if "session_id" not in item and "id" not in item and file_top_id:
                                        item["session_id"] = file_top_id
                                    if "cwd" not in item and file_top_cwd:
                                        item["cwd"] = file_top_cwd
                                    entries.append(item)
                except ValueError:
                    entries = []
        except OSError:
            continue

        file_sid = file_top_id
        file_cwd = file_top_cwd
        for entry in entries:
            p = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
            if not file_sid:
                cand_sid = (
                    p.get("id")
                    or p.get("session_id")
                    or p.get("sessionId")
                    or entry.get("id")
                    or entry.get("session_id")
                    or entry.get("sessionId")
                )
                if cand_sid:
                    file_sid = str(cand_sid)
            if not file_cwd:
                cand_cwd = (
                    p.get("cwd")
                    or p.get("working_directory")
                    or p.get("workdir")
                    or p.get("workspace")
                    or entry.get("cwd")
                    or entry.get("working_directory")
                    or entry.get("workdir")
                    or entry.get("workspace")
                )
                if isinstance(cand_cwd, str) and cand_cwd:
                    file_cwd = cand_cwd
            if file_sid and file_cwd:
                break

        sid = file_sid or default_sid
        cwd = file_cwd or ""

        for entry in entries:
            etype = entry.get("type")
            payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
            msg_dict = (
                entry.get("message")
                if isinstance(entry.get("message"), dict)
                else (payload.get("message") if isinstance(payload.get("message"), dict) else None)
            )

            if etype in ("session_meta", "session", "session_start"):
                entry_sid = (
                    payload.get("id")
                    or payload.get("session_id")
                    or payload.get("sessionId")
                    or entry.get("id")
                    or entry.get("session_id")
                    or entry.get("sessionId")
                )
            else:
                entry_sid = (
                    payload.get("session_id")
                    or payload.get("sessionId")
                    or entry.get("session_id")
                    or entry.get("sessionId")
                )

            if entry_sid:
                sid = str(entry_sid)

            entry_cwd = (
                payload.get("cwd")
                or payload.get("working_directory")
                or payload.get("workdir")
                or payload.get("workspace")
                or entry.get("cwd")
                or entry.get("working_directory")
                or entry.get("workdir")
                or entry.get("workspace")
            )
            if isinstance(entry_cwd, str) and entry_cwd:
                cwd = entry_cwd
                ws_name = os.path.basename(os.path.normpath(cwd))
                if sid in session_events_by_id and not session_events_by_id[sid].get("workspace"):
                    session_events_by_id[sid]["workspace"] = ws_name
                for ev in session_loads:
                    if ev.get("session_id") == sid and not ev.get("workspace"):
                        ev["workspace"] = ws_name

            ts = (
                _epoch_to_iso(entry.get("timestamp"))
                or _epoch_to_iso(payload.get("timestamp"))
                or (_epoch_to_iso(msg_dict.get("timestamp")) if msg_dict else None)
            )

            if etype in ("session_meta", "session", "session_start") and ts:
                if sid not in seen_sessions:
                    workspace = os.path.basename(os.path.normpath(cwd)) if cwd else ""
                    ev = {
                        "type": "session",
                        "ts": ts,
                        "session_id": sid,
                        "workspace": workspace,
                        "agent": "codex",
                    }
                    events.append(ev)
                    seen_sessions.add(sid)
                    session_events_by_id[sid] = ev
                continue

            if not ts:
                continue

            if sid not in seen_sessions:
                workspace = os.path.basename(os.path.normpath(cwd)) if cwd else ""
                ev = {
                    "type": "session",
                    "ts": ts,
                    "session_id": sid,
                    "workspace": workspace,
                    "agent": "codex",
                }
                events.append(ev)
                seen_sessions.add(sid)
                session_events_by_id[sid] = ev

            role = entry.get("role") or payload.get("role") or (msg_dict.get("role") if msg_dict else None)
            p_type = payload.get("type") if payload else ""
            is_user = (
                role == "user"
                or etype in ("user_message", "user_prompt", "user_input", "user")
                or (etype == "event_msg" and p_type in ("user_message", "user_prompt", "prompt", "user"))
            )
            if is_user:
                raw_content = (
                    (msg_dict.get("content") or msg_dict.get("text") or msg_dict.get("message") or msg_dict.get("prompt"))
                    if msg_dict
                    else None
                )
                if raw_content is None:
                    raw_content = (
                        payload.get("message")
                        or payload.get("content")
                        or payload.get("text")
                        or payload.get("prompt")
                        or entry.get("message")
                        or entry.get("content")
                        or entry.get("prompt")
                    )
                texts: list[str] = []
                if isinstance(raw_content, str):
                    texts.append(raw_content)
                elif isinstance(raw_content, list):
                    for part in raw_content:
                        if isinstance(part, str):
                            texts.append(part)
                        elif isinstance(part, dict):
                            part_text = part.get("text") or part.get("content")
                            if isinstance(part_text, str):
                                texts.append(part_text)
                for text in texts:
                    if detect._CODEX_HEADER_RE.search(text):
                        text = detect._CODEX_HEADER_RE.sub("", text, count=1).strip()
                    elif "## My request for Codex:" in text:
                        text = text.split("## My request for Codex:", 1)[1].strip()
                    text = text.strip()
                    if "/" not in text and "<command-name>" not in text:
                        continue
                    syn = {
                        "hook_event_name": "UserPromptSubmit",
                        "timestamp": ts,
                        "session_id": sid,
                        "cwd": cwd,
                        "prompt": text,
                        "agent": "codex",
                    }
                    rec = detect.detect(syn, lambda: known_names)
                    if rec:
                        events.append(rec)
                        session_loads.append(rec)

            tool_calls_to_process: list[tuple[str, dict]] = []

            if etype == "response_item" and p_type in ("function_call", "tool_call", "custom_tool_call", "tool_use", "call", "tool"):
                tc = _extract_codex_tool_call(payload)
                if tc:
                    tool_calls_to_process.append(tc)
            elif etype in ("function_call", "tool_call", "tool_use", "custom_tool_call", "call"):
                tc = _extract_codex_tool_call(entry)
                if tc:
                    tool_calls_to_process.append(tc)

            calls_list = (
                entry.get("tool_calls")
                or payload.get("tool_calls")
                or (msg_dict.get("tool_calls") if msg_dict else None)
            )
            if isinstance(calls_list, list):
                for c in calls_list:
                    tc = _extract_codex_tool_call(c)
                    if tc:
                        tool_calls_to_process.append(tc)

            fn_call = (
                entry.get("function_call")
                or payload.get("function_call")
                or (msg_dict.get("function_call") if msg_dict else None)
            )
            if isinstance(fn_call, dict):
                tc = _extract_codex_tool_call(fn_call)
                if tc:
                    tool_calls_to_process.append(tc)

            msg_content = (
                (msg_dict.get("content") if msg_dict else None)
                or payload.get("content")
                or entry.get("content")
            )
            if isinstance(msg_content, list):
                for part in msg_content:
                    if not isinstance(part, dict):
                        continue
                    ptype = part.get("type")
                    if ptype in ("tool_use", "tool_call", "function_call", "toolCall", "call"):
                        tc = _extract_codex_tool_call(part)
                        if tc:
                            tool_calls_to_process.append(tc)

            seen_calls: set[tuple[str, str]] = set()
            for tool_name, tool_input in tool_calls_to_process:
                call_key = (tool_name, json.dumps(tool_input, sort_keys=True))
                if call_key in seen_calls:
                    continue
                seen_calls.add(call_key)
                syn = {
                    "hook_event_name": "PostToolUse",
                    "timestamp": ts,
                    "session_id": sid,
                    "cwd": cwd,
                    "agent": "codex",
                    "tool_name": tool_name,
                    "tool_input": tool_input,
                }
                rec = detect.detect(syn, lambda: known_names)
                if rec:
                    if rec.get("source") == "agent-read" and _should_skip_recent_read(
                        recent_reads, sid, rec["skill"], rec["ts"]
                    ):
                        continue
                    events.append(rec)
                    session_loads.append(rec)

        if cwd:
            ws_name = os.path.basename(os.path.normpath(cwd))
            if sid in session_events_by_id and not session_events_by_id[sid].get("workspace"):
                session_events_by_id[sid]["workspace"] = ws_name
            for ev in session_loads:
                if ev.get("session_id") == sid and not ev.get("workspace"):
                    ev["workspace"] = ws_name

    return events


def sync_events(
    data_dir: Path | None = None,
    home: Path | None = None,
    workspaces: list[Path] | None = None,
    agents: Iterable[str] | None = None,
) -> dict[str, int]:
    resolved_home = home or Path.home()
    if data_dir is not None:
        base = data_dir
    elif os.environ.get("SKILL_USAGE_DATA"):
        base = Path(os.environ["SKILL_USAGE_DATA"])
    else:
        base = resolved_home / ".copilot" / "skill-usage" / "data"

    ws_list = workspaces or []
    names = inventory.known_names(resolved_home, ws_list)
    existing = store.read_events(base)

    existing_sessions: set[tuple[str, str]] = set()
    existing_exact: set[tuple[str, str, str, str, str, str]] = set()
    existing_load_times: dict[tuple[str, str, str, str], list[datetime]] = {}

    for ev in existing:
        ag = str(ev.get("agent") or "copilot")
        sid = str(ev.get("session_id") or "")
        etype = str(ev.get("type") or "")
        ts_str = str(ev.get("ts") or "")
        skill = str(ev.get("skill") or "")
        source = str(ev.get("source") or "")
        if etype == "session" and sid:
            existing_sessions.add((ag, sid))
        existing_exact.add((ag, sid, etype, ts_str, skill, source))
        if etype == "load":
            stamp = parse_ts(ts_str)
            if stamp is not None:
                existing_load_times.setdefault((ag, sid, skill.lower(), source), []).append(stamp)

    target_agents = tuple(agents) if agents is not None else SYNC_AGENTS
    collectors = {
        "claude": collect_claude_events,
        "antigravity": collect_antigravity_events,
        "pi": collect_pi_events,
        "hermes": collect_hermes_events,
        "codex": collect_codex_events,
    }
    counts = {a: 0 for a in SYNC_AGENTS}

    for ag in target_agents:
        collector = collectors.get(ag)
        if collector is None:
            continue
        for ev in collector(resolved_home, names):
            sid = str(ev.get("session_id") or "")
            etype = str(ev.get("type") or "")
            ts_str = str(ev.get("ts") or "")
            skill = str(ev.get("skill") or "")
            source = str(ev.get("source") or "")

            if etype == "session":
                if sid and (ag, sid) in existing_sessions:
                    continue
                if sid:
                    existing_sessions.add((ag, sid))
            else:
                exact_key = (ag, sid, etype, ts_str, skill, source)
                if exact_key in existing_exact:
                    continue
                stamp = parse_ts(ts_str)
                load_key = (ag, sid, skill.lower(), source)
                if stamp is not None:
                    prior = existing_load_times.get(load_key, [])
                    if any(abs((stamp - p).total_seconds()) <= _LIVE_HOOK_SLACK_SECONDS for p in prior):
                        continue
                    existing_load_times.setdefault(load_key, []).append(stamp)
                existing_exact.add(exact_key)

            store.append_event(ev, base)
            counts[ag] = counts.get(ag, 0) + 1

    counts["total"] = sum(counts[a] for a in SYNC_AGENTS if a in counts)
    return counts


def _upsert_claude_hook_list(entries: list, hook_cmd: str, matcher: str | None = None) -> list:
    cleaned: list = []
    for item in entries:
        if not isinstance(item, dict):
            cleaned.append(item)
            continue
        inner = item.get("hooks")
        if isinstance(inner, list):
            kept = [
                h
                for h in inner
                if not (isinstance(h, dict) and "hook.py" in str(h.get("command", "")))
            ]
            if kept:
                updated = dict(item)
                updated["hooks"] = kept
                cleaned.append(updated)
        elif "hook.py" not in str(item.get("command", "")):
            cleaned.append(item)
    new_entry: dict = {"hooks": [{"type": "command", "command": hook_cmd}]}
    if matcher is not None:
        new_entry["matcher"] = matcher
    cleaned.append(new_entry)
    return cleaned


def install_hooks(
    home: Path | None = None,
    repo_dir: Path | None = None,
    agents: Iterable[str] | None = None,
) -> dict[str, str]:
    resolved_home = home or Path.home()
    resolved_repo = (repo_dir or Path(__file__).resolve().parent.parent).resolve()
    hook_script = resolved_repo / "hook.py"
    python_exe = sys.executable or "python3"
    cmd = f'"{python_exe}" "{hook_script}"'
    targets = tuple(agents) if agents is not None else SYNC_AGENTS
    installed: dict[str, str] = {}

    if "claude" in targets:
        claude_dir = resolved_home / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        settings_path = claude_dir / "settings.json"
        settings_data: dict = {}
        if settings_path.is_file():
            try:
                loaded = json.loads(settings_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    settings_data = loaded
            except (OSError, ValueError):
                settings_data = {}
        hooks_map = settings_data.get("hooks")
        if not isinstance(hooks_map, dict):
            hooks_map = {}
        claude_cmd = f'{cmd} --agent claude'
        hooks_map["SessionStart"] = _upsert_claude_hook_list(
            hooks_map.get("SessionStart") if isinstance(hooks_map.get("SessionStart"), list) else [],
            claude_cmd,
        )
        hooks_map["UserPromptSubmit"] = _upsert_claude_hook_list(
            hooks_map.get("UserPromptSubmit") if isinstance(hooks_map.get("UserPromptSubmit"), list) else [],
            claude_cmd,
        )
        hooks_map["PostToolUse"] = _upsert_claude_hook_list(
            hooks_map.get("PostToolUse") if isinstance(hooks_map.get("PostToolUse"), list) else [],
            claude_cmd,
            matcher="Read|Skill",
        )
        settings_data["hooks"] = hooks_map
        settings_path.write_text(json.dumps(settings_data, indent=2) + "\n", encoding="utf-8")
        installed["claude"] = str(settings_path)

    if "antigravity" in targets:
        config_dir = resolved_home / ".gemini" / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        hooks_path = config_dir / "hooks.json"
        hooks_data: dict = {}
        if hooks_path.is_file():
            try:
                loaded = json.loads(hooks_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    hooks_data = loaded
            except (OSError, ValueError):
                hooks_data = {}
        hooks_data["skill-usage"] = {
            "enabled": True,
            "PreInvocation": [{"type": "command", "command": cmd, "timeout": 5}],
            "PreToolUse": [
                {
                    "matcher": "view_file",
                    "hooks": [{"type": "command", "command": cmd, "timeout": 5}],
                }
            ],
        }
        hooks_path.write_text(json.dumps(hooks_data, indent=2) + "\n", encoding="utf-8")
        installed["antigravity"] = str(hooks_path)

    if "pi" in targets:
        ext_dir = resolved_home / ".pi" / "agent" / "extensions"
        ext_dir.mkdir(parents=True, exist_ok=True)
        ext_path = ext_dir / "skill-usage.ts"
        ext_code = f'''import {{ spawn }} from "node:child_process";

const PYTHON = {json.dumps(python_exe)};
const HOOK = {json.dumps(str(hook_script))};

function send(payload: Record<string, unknown>) {{
  try {{
    const child = spawn(PYTHON, [HOOK], {{ stdio: ["pipe", "ignore", "ignore"] }});
    child.stdin.write(JSON.stringify({{ ...payload, agent: "pi", timestamp: new Date().toISOString() }}));
    child.stdin.end();
    child.unref();
  }} catch {{
    // Ignore errors so Pi is never blocked
  }}
}}

export default function (pi: any) {{
  pi.on("session_start", async (_event: any, ctx: any) => {{
    send({{
      hook_event_name: "SessionStart",
      session_id: ctx?.sessionManager?.getSessionId?.() ?? "",
      cwd: process.cwd(),
    }});
  }});

  pi.on("input", async (event: any, ctx: any) => {{
    if (typeof event?.text === "string" && event.text.trim().startsWith("/")) {{
      send({{
        hook_event_name: "UserPromptSubmit",
        session_id: ctx?.sessionManager?.getSessionId?.() ?? "",
        cwd: process.cwd(),
        prompt: event.text,
      }});
    }}
  }});

  pi.on("tool_call", async (event: any, ctx: any) => {{
    if (event?.toolName === "read") {{
      send({{
        hook_event_name: "PostToolUse",
        session_id: ctx?.sessionManager?.getSessionId?.() ?? "",
        cwd: process.cwd(),
        tool_name: event.toolName,
        tool_input: event.input ?? {{}},
      }});
    }}
  }});
}}
'''
        ext_path.write_text(ext_code, encoding="utf-8")
        installed["pi"] = str(ext_path)

    if "hermes" in targets:
        plugin_dir = resolved_home / ".hermes" / "plugins" / "skill-usage"
        plugin_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = plugin_dir / "plugin.yaml"
        manifest_path.write_text(
            "name: skill-usage\nversion: 1.0.0\ndescription: Local skill usage tracking hook\n",
            encoding="utf-8",
        )
        init_path = plugin_dir / "__init__.py"
        init_code = f'''from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_DIR = Path({json.dumps(str(resolved_repo))})
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))


def _record(payload: dict) -> None:
    try:
        import hook
        payload.setdefault("agent", "hermes")
        payload.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        payload.setdefault("cwd", os.getcwd())
        hook.run(payload)
    except Exception:
        pass


def register(ctx) -> None:
    def on_session_start(session_id: str = "", **kwargs) -> None:
        _record({{"hook_event_name": "SessionStart", "session_id": str(session_id or "")}})

    def post_tool_call(tool_name: str = "", args: dict | None = None, session_id: str = "", **kwargs) -> None:
        if tool_name in ("skill_view", "read_file", "view_file", "read"):
            _record({{
                "hook_event_name": "PostToolUse",
                "session_id": str(session_id or ""),
                "tool_name": tool_name,
                "tool_input": args if isinstance(args, dict) else {{}},
            }})

    try:
        ctx.register_hook("on_session_start", on_session_start)
        ctx.register_hook("post_tool_call", post_tool_call)
    except Exception:
        pass
'''
        init_path.write_text(init_code, encoding="utf-8")
        installed["hermes"] = str(init_path)

    if "codex" in targets:
        codex_dir = resolved_home / ".codex"
        codex_dir.mkdir(parents=True, exist_ok=True)
        hooks_path = codex_dir / "hooks.json"
        hooks_data: dict = {}
        if hooks_path.is_file():
            try:
                loaded = json.loads(hooks_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    hooks_data = loaded
            except (OSError, ValueError):
                hooks_data = {}
        codex_cmd = f'{cmd} --agent codex'
        hooks_target = hooks_data["hooks"] if isinstance(hooks_data.get("hooks"), dict) else hooks_data
        hooks_target["SessionStart"] = _upsert_claude_hook_list(
            hooks_target.get("SessionStart") if isinstance(hooks_target.get("SessionStart"), list) else [],
            codex_cmd,
        )
        hooks_target["UserPromptSubmit"] = _upsert_claude_hook_list(
            hooks_target.get("UserPromptSubmit") if isinstance(hooks_target.get("UserPromptSubmit"), list) else [],
            codex_cmd,
        )
        hooks_target["PostToolUse"] = _upsert_claude_hook_list(
            hooks_target.get("PostToolUse") if isinstance(hooks_target.get("PostToolUse"), list) else [],
            codex_cmd,
            matcher=".*(?:read_file|view_file|read|view|readFile|Read|Skill|skill|skill_view|file_read|file_view)",
        )
        hooks_path.write_text(json.dumps(hooks_data, indent=2) + "\n", encoding="utf-8")
        installed["codex"] = str(hooks_path)

    return installed

