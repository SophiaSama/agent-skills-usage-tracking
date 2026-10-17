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

SYNC_AGENTS = ("claude", "antigravity", "pi", "hermes")
_USER_REQ_RE = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.DOTALL)
_PI_SKILL_CMD_RE = re.compile(rf"^\s*/skill:({detect._NAME})(?=\s|$)")
_HERMES_INVOKE_RE = re.compile(r'^\[IMPORTANT: The user has invoked the "([^"]+)"')
_DEDUP_READ_SECONDS = 60
_LIVE_HOOK_SLACK_SECONDS = 5


def _epoch_to_iso(value) -> str | None:
    if isinstance(value, (int, float)) and value > 0:
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
        except (OSError, OverflowError, ValueError):
            return None
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
        except (OSError, OverflowError, ValueError):
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
                        step_ts = step.get("created_at")
                        if isinstance(step_ts, str) and step_ts and first_ts is None:
                            first_ts = step_ts
                        step_type = step.get("type")
                        if step_type == "USER_INPUT":
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
                        elif step_type == "PLANNER_RESPONSE":
                            for tc in step.get("tool_calls") or []:
                                if not isinstance(tc, dict):
                                    continue
                                args = tc.get("args")
                                if isinstance(args, dict) and not cwd:
                                    raw_cwd = detect._clean_str(args.get("Cwd"))
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
                            for text in texts:
                                m = _PI_SKILL_CMD_RE.match(text)
                                if m:
                                    workspace = os.path.basename(os.path.normpath(cwd)) if cwd else ""
                                    events.append(
                                        {
                                            "type": "load",
                                            "ts": ts,
                                            "session_id": session_id,
                                            "workspace": workspace,
                                            "agent": "pi",
                                            "skill": m.group(1),
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
                                syn = {
                                    "hook_event_name": "PostToolUse",
                                    "timestamp": ts,
                                    "session_id": session_id,
                                    "cwd": cwd,
                                    "agent": "pi",
                                    "tool_name": part.get("name"),
                                    "tool_input": part.get("arguments") if isinstance(part.get("arguments"), dict) else {},
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
                conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            except sqlite3.Error:
                conn = sqlite3.connect(str(db_path))
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
            query = (
                "SELECT session_id, role, content, tool_calls, timestamp "
                "FROM messages WHERE COALESCE(active, 1) = 1 ORDER BY timestamp, id"
            )
            for row in conn.execute(query):
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
                        skill_name = name_match.group(1) if name_match else raw_skill
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
                count = max(int(meta.get("use_count") or 0), int(meta.get("view_count") or 0))
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
                            if "/" not in text and "<command-name>" not in text:
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
                            raw_input = part.get("input") if isinstance(part.get("input"), dict) else part.get("arguments")
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
                existing_load_times.setdefault((ag, sid, skill, source), []).append(stamp)

    target_agents = tuple(agents) if agents is not None else SYNC_AGENTS
    collectors = {
        "claude": collect_claude_events,
        "antigravity": collect_antigravity_events,
        "pi": collect_pi_events,
        "hermes": collect_hermes_events,
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
                if (ag, sid) in existing_sessions:
                    continue
                existing_sessions.add((ag, sid))
            else:
                exact_key = (ag, sid, etype, ts_str, skill, source)
                if exact_key in existing_exact:
                    continue
                stamp = parse_ts(ts_str)
                load_key = (ag, sid, skill, source)
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

    return installed

