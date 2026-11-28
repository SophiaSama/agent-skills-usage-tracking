"""Follow-through: after a skill loads, does the agent use the skill's own files?

A skill with support files (``scripts/``, ``references/``, ...) usually tells the
agent to read or run them. A load followed by no access to any of those files
suggests the skill was ignored, loaded for the wrong task, or only needed its
``SKILL.md``. This is a signal for review, not a verdict on quality.

Analysis runs on demand over local transcripts and stores nothing. Output holds
skill names and counts only.
"""

from __future__ import annotations

import csv
import json
import os
import posixpath
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from tracker import detect, inventory
from tracker.report import parse_ts

FOLLOW_AGENTS = ("antigravity", "claude", "pi", "hermes", "codex")
COMMAND_TOOLS = {
    "run_command",
    "bash",
    "shell",
    "local_shell",
    "shell_command",
    "exec",
    "exec_command",
    "run_terminal_cmd",
    "execute_command",
    "terminal",
}
COMMAND_KEYS = ("CommandLine", "command", "cmd")
PROMPT_TOOL = "__user_prompt__"
DEFAULT_MIN_SESSIONS = 5
DEFAULT_LOW_RATE = 0.3
_MAX_SUPPORT_SCAN = 200
# Files that describe or package a skill rather than instruct the agent; never "support".
METADATA_DIRS = {"agents"}  # e.g. agents/openai.yaml runtime metadata
METADATA_PREFIXES = ("license", "readme", "changelog")
METADATA_FILES = {"package.json", "package-lock.json", "pyproject.toml", "plugin.json", "manifest.json"}

_USER_REQ_RE = re.compile(
    r"<\s*(?:USER_REQUEST|USER_PROMPT|USER_MESSAGE)(?:\s+[^>]*)?>\s*(.*?)\s*<\s*/(?:USER_REQUEST|USER_PROMPT|USER_MESSAGE)\s*>",
    re.DOTALL | re.IGNORECASE,
)
_PI_SKILL_CMD_RE = re.compile(rf"^\s*/skill:({detect._NAME})(?=\s|$)", re.IGNORECASE)
_HERMES_INVOKE_RE = re.compile(r'^\[IMPORTANT: The user has invoked the "([^"]+)"')
_NON_USER_STEP_TYPES = {"PLANNER_RESPONSE", "AGENT_RESPONSE", "MODEL_RESPONSE", "TOOL_EXECUTION", "TOOL_RESULT"}
_SUPPORT_DIR_ALT = "|".join(re.escape(d) for d in sorted(inventory.SUPPORT_DIRS))


@dataclass(frozen=True)
class ToolCall:
    ts: str | None
    tool: str
    args: dict


@dataclass
class Session:
    agent: str
    session_id: str
    ts: datetime | None
    calls: list = field(default_factory=list)


@dataclass
class SkillFollow:
    name: str
    matched: bool = True
    sessions_loaded: int = 0
    sessions_scored: int = 0  # loads of a copy that has support files to follow through on
    sessions_followed: int = 0
    kinds: dict = field(default_factory=lambda: {"reference": 0, "script": 0})

    @property
    def has_support(self) -> bool:
        return self.sessions_scored > 0

    @property
    def rate(self) -> float | None:
        return self.sessions_followed / self.sessions_scored if self.sessions_scored else None


@dataclass(frozen=True)
class Support:
    own_files: bool  # files in the skill folder besides SKILL.md
    linked: frozenset  # existing files linked from SKILL.md, outside the folder or not

    @property
    def any(self) -> bool:
        return self.own_files or bool(self.linked)


def prompt_call(prompt: str, ts: str | None = None, skill: str | None = None) -> ToolCall:
    args = {"prompt": prompt}
    if skill:
        args["skill"] = skill
    return ToolCall(ts, PROMPT_TOOL, args)


# ---------------------------------------------------------------------------
# Support files: what a loaded skill could be followed through on


_LINK_RE = re.compile(r"\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")


def _is_metadata(rel_dir: str, fname: str) -> bool:
    lower = fname.lower()
    norm_dir = rel_dir.replace("\\", "/").strip("/")
    if norm_dir in ("", ".") and lower == "skill.md":
        return True
    first_dir = norm_dir.split("/")[0].lower() if norm_dir not in ("", ".") else ""
    if first_dir in METADATA_DIRS:
        return True
    return lower.startswith(METADATA_PREFIXES) or lower in METADATA_FILES


def has_support_files(skill_md_path: str) -> bool:
    root = Path(skill_md_path).parent
    seen = 0
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in inventory.SKIP_DIRS and not d.startswith(".")]
            rel_dir = os.path.relpath(dirpath, root)
            for fname in filenames:
                seen += 1
                if seen > _MAX_SUPPORT_SCAN:
                    return True
                if fname.startswith(".") or _is_metadata(rel_dir, fname):
                    continue
                return True
    except OSError:
        return False
    return False


def linked_files(skill_md_path: str) -> frozenset:
    """Existing local files that SKILL.md links to with markdown links."""
    md = Path(skill_md_path)
    try:
        text = md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return frozenset()
    out = set()
    for target in _LINK_RE.findall(text):
        if re.match(r"^[a-z][a-z0-9+.-]*://", target, re.IGNORECASE) and not target.lower().startswith("file://"):
            continue
        target = re.sub(r"^file://", "", target, flags=re.IGNORECASE).split("#")[0]
        if not target or target.startswith("mailto:"):
            continue
        path = Path(os.path.expanduser(target))
        if not path.is_absolute():
            path = md.parent / path
        try:
            resolved = os.path.normpath(str(path))
            if Path(resolved).is_file() and os.path.normcase(resolved) != os.path.normcase(os.path.normpath(str(md))):
                out.add(resolved)
        except OSError:
            continue
    return frozenset(out)


class SupportIndex:
    """Support per loaded SKILL.md path, falling back to inventory copies by name."""

    def __init__(self, copies):
        self._cache: dict[str, Support] = {}
        self._by_name: dict[str, list[str]] = {}
        for copy in copies:
            self._by_name.setdefault(copy.name.lower(), []).append(copy.path)

    def for_path(self, path: str) -> Support:
        key = os.path.normpath(path)
        if key not in self._cache:
            self._cache[key] = Support(has_support_files(key), linked_files(key))
        return self._cache[key]

    def for_load(self, name: str, path: str | None) -> Support:
        if path and os.path.isfile(path):
            return self.for_path(path)
        supports = [self.for_path(p) for p in self._by_name.get(name.lower(), [])]
        return Support(
            any(s.own_files for s in supports),
            frozenset().union(*[s.linked for s in supports]) if supports else frozenset(),
        )


def support_map(copies) -> dict[str, bool]:
    """Lowercased skill name -> whether any copy has support files."""
    index = SupportIndex(copies)
    return {copy.name.lower(): index.for_load(copy.name, None).any for copy in copies}


# ---------------------------------------------------------------------------
# Detection


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def _tool_base(tool: str) -> str:
    return tool.split(":")[-1].split("/")[-1].split(".")[-1].split("__")[-1].lower() if isinstance(tool, str) else ""


def _skill_file_re(name: str) -> re.Pattern:
    # A path inside the skill folder (allowing category subdirs under skills/, e.g. ~/.hermes/skills/cat/<name>/...).
    return re.compile(
        rf"(?:^|[/\\])skills[/\\](?:(?!(?:{_SUPPORT_DIR_ALT})[/\\])[^/\\\s'\"`;|&<>()]+[/\\])*{re.escape(name)}[/\\](?P<rest>[^\s'\"`;|&<>()]+)",
        re.IGNORECASE,
    )


def _is_command(call: ToolCall) -> bool:
    base = _tool_base(call.tool)
    return base in COMMAND_TOOLS or any(isinstance(call.args.get(k), (str, list)) for k in COMMAND_KEYS)


def _path_forms(path: str) -> tuple:
    home = os.path.expanduser("~")
    forms = [path, path.replace("\\", "/")]
    if path.startswith(home + os.sep):
        forms.append("~" + path[len(home):])
    return tuple(dict.fromkeys(forms))


def _classify_skill_relpath(rel_path: str, is_cmd: bool = False) -> str | None:
    cleaned = rel_path.split("#", 1)[0].replace("\\", "/").strip("/")
    if not cleaned:
        return None
    norm = posixpath.normpath(cleaned)
    if norm in ("", ".", "..") or norm.startswith("../"):
        return None
    parts = norm.split("/")
    if any(p.startswith(".") or p in inventory.SKIP_DIRS for p in parts):
        return None
    rel_dir = posixpath.dirname(norm) or "."
    fname = posixpath.basename(norm)
    if _is_metadata(rel_dir, fname):
        return None
    if is_cmd or norm.lower().startswith("scripts/"):
        return "script"
    return "reference"


def _skill_view_kind(call: ToolCall, name: str) -> str | None:
    if _tool_base(call.tool) != "skill_view":
        return None
    matched_skill = False
    for key in detect.SKILL_NAME_KEYS:
        raw_val = detect._clean_str(call.args.get(key))
        if not raw_val:
            continue
        m = detect.NAME_RE.match(raw_val)
        candidate = m.group(1) if m else raw_val
        if candidate.lower() == name.lower():
            matched_skill = True
            break
    if not matched_skill:
        return None
    raw_fp = detect._clean_str(call.args.get("file_path"))
    if not raw_fp:
        return None
    match = _skill_file_re(name).search(raw_fp)
    rel = match.group("rest") if match else raw_fp
    return _classify_skill_relpath(rel, is_cmd=False)


def follow_kind(call: ToolCall, name: str, linked=frozenset()) -> str | None:
    """Return 'script' or 'reference' if the call touches the skill's own or linked files."""
    if call.tool == PROMPT_TOOL:
        return None
    if _tool_base(call.tool) == "skill_view":
        return _skill_view_kind(call, name)
    is_cmd = _is_command(call)
    pattern = _skill_file_re(name)
    texts = list(_strings(call.args))
    for text in texts:
        for match in pattern.finditer(text):
            kind = _classify_skill_relpath(match.group("rest"), is_cmd=is_cmd)
            if kind:
                return kind
    for target in linked:
        if any(form in text for text in texts for form in _path_forms(target)):
            return "script" if is_cmd else "reference"
    return None


def load_of(call: ToolCall, known_names) -> tuple[str, str] | None:
    """(skill name, SKILL.md path or '') loaded by this call, using the usage report's rules."""
    if call.tool == PROMPT_TOOL:
        explicit = detect._clean_str(call.args.get("skill"))
        if explicit:
            name_map = {n.lower(): n for n in known_names}
            return name_map.get(explicit.lower(), explicit), ""
        prompt = call.args.get("prompt")
        if isinstance(prompt, str):
            rec = detect.detect({"hook_event_name": "UserPromptSubmit", "prompt": prompt}, lambda: known_names)
            if rec and rec.get("type") == "load":
                return rec["skill"], rec.get("path") or ""
        return None
    rec = detect.detect({"toolCall": {"name": call.tool, "args": call.args}}, lambda: known_names)
    if rec and rec.get("type") == "load":
        return rec["skill"], rec.get("path") or ""
    return None


def analyze(sessions, copies, now: datetime | None = None, days: int | None = None, agent: str | None = None):
    index = SupportIndex(copies)
    canonical = {}
    for copy in copies:
        canonical.setdefault(copy.name.lower(), copy.name)
    known = set(canonical.values())
    cutoff = now - timedelta(days=days) if now is not None and days is not None else None
    agent_filter = agent if agent and agent != "all" else None

    rows: dict[str, SkillFollow] = {}
    for session in sessions:
        if agent_filter and session.agent != agent_filter:
            continue
        if cutoff is not None and (session.ts is None or session.ts < cutoff):
            continue
        # skill key -> (support of the loaded copy, has_specific_path, follow kinds seen after that load)
        loaded: dict[str, tuple[Support, bool, set]] = {}
        for call in session.calls:
            for key, (support, _, kinds) in loaded.items():
                kind = follow_kind(call, canonical.get(key, key), support.linked)
                if kind:
                    kinds.add(kind)
            hit = load_of(call, known)
            if hit:
                skey = hit[0].lower()
                has_path = bool(hit[1] and os.path.isfile(hit[1]))
                if skey not in loaded:
                    loaded[skey] = (index.for_load(hit[0], hit[1]), has_path, set())
                elif has_path and not loaded[skey][1]:
                    loaded[skey] = (index.for_load(hit[0], hit[1]), True, loaded[skey][2])
        for key, (support, _, kinds) in loaded.items():
            row = rows.setdefault(key, SkillFollow(canonical.get(key, key), matched=key in canonical))
            row.sessions_loaded += 1
            if not support.any:
                continue
            row.sessions_scored += 1
            if kinds:
                row.sessions_followed += 1
                for kind in kinds:
                    row.kinds[kind] += 1
    return sorted(rows.values(), key=lambda r: (-r.sessions_loaded, r.name.lower()))


# ---------------------------------------------------------------------------
# Transcript readers


def _json_args(raw) -> dict:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    return raw if isinstance(raw, dict) else {}


def _session_ts(first: str | None) -> datetime | None:
    return parse_ts(first) if first else None


def _extract_agy_prompt(content: str) -> str:
    match = _USER_REQ_RE.search(content)
    return match.group(1).strip() if match else content.strip()


def _user_texts(content) -> list[str]:
    texts: list[str] = []
    if isinstance(content, str):
        texts.append(content)
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, str):
                texts.append(part)
            elif isinstance(part, dict):
                part_text = part.get("text") if part.get("type") == "text" else (part.get("text") or part.get("content"))
                if isinstance(part_text, str):
                    texts.append(part_text)
    return texts


def iter_antigravity_sessions(home: Path):
    for product in ("antigravity", "antigravity-ide", "antigravity-cli"):
        brain = home / ".gemini" / product / "brain"
        for conv in inventory._iterdir(brain):
            if not inventory._is_dir(conv) or conv.name.startswith("."):
                continue
            transcript = conv / ".system_generated" / "logs" / "transcript.jsonl"
            if not inventory._is_file(transcript):
                alt1 = conv / "transcript.jsonl"
                alt2 = conv / "logs" / "transcript.jsonl"
                if inventory._is_file(alt1):
                    transcript = alt1
                elif inventory._is_file(alt2):
                    transcript = alt2
                else:
                    continue
            session = Session("antigravity", conv.name, None)
            first = None
            try:
                with open(transcript, encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        try:
                            step = json.loads(line)
                        except ValueError:
                            continue
                        if not isinstance(step, dict):
                            continue
                        ts = detect._epoch_to_iso(step.get("created_at") or step.get("timestamp"))
                        first = first or ts
                        step_type = str(step.get("type") or "")
                        tcs = step.get("tool_calls") or step.get("toolCalls") or []
                        is_user_step = step_type in ("USER_INPUT", "USER", "user", "USER_MESSAGE") or (
                            step_type not in _NON_USER_STEP_TYPES
                            and step.get("role") in (None, "user", "human")
                            and not tcs
                        )
                        if is_user_step and isinstance(step.get("content"), str):
                            prompt = _extract_agy_prompt(step["content"])
                            if "/" in prompt or "<command-name>" in prompt.lower():
                                session.calls.append(prompt_call(prompt, ts))
                        for tc in tcs:
                            if isinstance(tc, dict) and isinstance(tc.get("name"), str):
                                session.calls.append(ToolCall(ts, tc["name"], _json_args(tc.get("args"))))
            except OSError:
                continue
            session.ts = _session_ts(first)
            yield session


def iter_claude_sessions(home: Path):
    root = home / ".claude" / "projects"
    if not inventory._is_dir(root):
        return
    try:
        files = sorted(root.rglob("*.jsonl"))
    except OSError:
        return
    sessions: dict[str, Session] = {}
    firsts: dict[str, str] = {}
    for path in files:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(entry, dict):
                        continue
                    sid = str(entry.get("sessionId") or entry.get("session_id") or path.stem)
                    msg = entry.get("message")
                    ts = detect._epoch_to_iso(entry.get("timestamp"))
                    if not ts and isinstance(msg, dict):
                        ts = detect._epoch_to_iso(msg.get("timestamp"))
                    if ts and sid not in firsts:
                        firsts[sid] = ts
                    if not isinstance(msg, dict):
                        continue
                    role = msg.get("role") or entry.get("type")
                    content = msg.get("content")
                    if role == "user":
                        for text in _user_texts(content):
                            if "/" in text or "<command-name>" in text.lower():
                                session = sessions.setdefault(sid, Session("claude", sid, None))
                                session.calls.append(prompt_call(text, ts))
                    elif role == "assistant" and isinstance(content, list):
                        for part in content:
                            if not isinstance(part, dict) or part.get("type") not in ("tool_use", "toolCall"):
                                continue
                            if not isinstance(part.get("name"), str):
                                continue
                            raw = part.get("input") if part.get("input") is not None else part.get("arguments")
                            session = sessions.setdefault(sid, Session("claude", sid, None))
                            session.calls.append(ToolCall(ts, part["name"], _json_args(raw)))
        except OSError:
            continue
    for sid, session in sessions.items():
        session.ts = _session_ts(firsts.get(sid))
        yield session


def iter_pi_sessions(home: Path):
    root = home / ".pi" / "agent" / "sessions"
    if not inventory._is_dir(root):
        return
    try:
        files = sorted(root.rglob("*.jsonl"))
    except OSError:
        return
    for path in files:
        sid = path.stem
        first = None
        calls: list[ToolCall] = []
        saw_entry = False
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
                    ts = detect._epoch_to_iso(entry.get("timestamp"))
                    if etype == "session":
                        sid = str(entry.get("id") or sid)
                        first = first or ts
                        saw_entry = True
                    elif etype == "message":
                        msg = entry.get("message")
                        if not isinstance(msg, dict):
                            continue
                        ts = ts or detect._epoch_to_iso(msg.get("timestamp"))
                        first = first or ts
                        saw_entry = True
                        role = msg.get("role")
                        content = msg.get("content")
                        if role == "user":
                            for text in _user_texts(content):
                                m = _PI_SKILL_CMD_RE.match(text)
                                if m:
                                    calls.append(prompt_call(text, ts, skill=m.group(1)))
                                elif "/" in text or "<command-name>" in text.lower():
                                    calls.append(prompt_call(text, ts))
                        elif role == "assistant" and isinstance(content, list):
                            for part in content:
                                if not isinstance(part, dict) or part.get("type") not in ("toolCall", "tool_use"):
                                    continue
                                if not isinstance(part.get("name"), str):
                                    continue
                                raw = part.get("arguments") if part.get("arguments") is not None else part.get("input")
                                calls.append(ToolCall(ts, part["name"], _json_args(raw)))
        except OSError:
            continue
        if saw_entry:
            yield Session("pi", sid, _session_ts(first), calls)


def iter_hermes_sessions(home: Path):
    db_path = home / ".hermes" / "state.db"
    if not inventory._is_file(db_path):
        return
    conn = None
    sessions: dict[str, Session] = {}
    firsts: dict[str, str] = {}
    try:
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30.0)
        except sqlite3.Error:
            conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True, timeout=30.0)
        conn.row_factory = sqlite3.Row

        try:
            sess_rows = conn.execute("SELECT id, started_at FROM sessions")
        except sqlite3.OperationalError:
            sess_rows = []
        for row in sess_rows:
            sid = str(row["id"] or "")
            if not sid:
                continue
            ts = detect._epoch_to_iso(row["started_at"])
            sessions.setdefault(sid, Session("hermes", sid, None))
            if ts and sid not in firsts:
                firsts[sid] = ts

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
            if not sid:
                continue
            ts = detect._epoch_to_iso(row["timestamp"])
            if ts and sid not in firsts:
                firsts[sid] = ts
            session = sessions.setdefault(sid, Session("hermes", sid, None))
            role = row["role"]
            content = row["content"]
            tool_calls_raw = row["tool_calls"]

            if role == "user" and isinstance(content, str) and content:
                m = _HERMES_INVOKE_RE.match(content)
                if m:
                    raw_skill = m.group(1).strip().lstrip("/")
                    name_match = detect.NAME_RE.match(raw_skill)
                    raw_name = name_match.group(1) if name_match else raw_skill
                    if raw_name:
                        session.calls.append(prompt_call(content, ts, skill=raw_name))
                elif content.lstrip().startswith("/") or "<command-name>" in content.lower():
                    session.calls.append(prompt_call(content, ts))

            if role == "assistant" and tool_calls_raw:
                try:
                    calls = json.loads(tool_calls_raw) if isinstance(tool_calls_raw, str) else tool_calls_raw
                except ValueError:
                    calls = []
                if isinstance(calls, list):
                    for call in calls:
                        if not isinstance(call, dict):
                            continue
                        fn = call.get("function") if isinstance(call.get("function"), dict) else call
                        fn_name = fn.get("name")
                        if not isinstance(fn_name, str) or not fn_name:
                            continue
                        raw_args = fn.get("arguments") if fn.get("arguments") is not None else fn.get("args")
                        session.calls.append(ToolCall(ts, fn_name, _json_args(raw_args)))
    except sqlite3.Error:
        return
    finally:
        if conn is not None:
            conn.close()

    for sid, session in sessions.items():
        session.ts = _session_ts(firsts.get(sid))
        yield session


def _extract_codex_tool_call(d: dict) -> tuple[str, dict] | None:
    if not isinstance(d, dict):
        return None
    fn = d.get("function") or d.get("call") or d.get("tool")
    if isinstance(fn, dict):
        name = fn.get("name") or fn.get("tool_name") or d.get("name") or d.get("tool_name")
        args = None
        for src in (fn, d):
            for k in ("arguments", "args", "input", "parameters", "params"):
                if src.get(k) is not None:
                    args = src.get(k)
                    break
            if args is not None:
                break
    else:
        name = d.get("name") or d.get("tool_name") or d.get("tool")
        args = None
        for k in ("arguments", "args", "input", "parameters", "params"):
            if d.get(k) is not None:
                args = d.get(k)
                break

    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = {}
    elif args is None:
        args = {}
    return (str(name), args) if isinstance(name, str) and name and isinstance(args, dict) else None


def _read_codex_entries(path: Path) -> tuple[list[dict], str]:
    entries: list[dict] = []
    file_top_id = ""
    with open(path, encoding="utf-8", errors="replace") as fh:
        content = fh.read()
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if not isinstance(parsed, dict):
            continue
        sub_list = (
            parsed.get("events")
            or parsed.get("messages")
            or parsed.get("items")
            or parsed.get("steps")
            or parsed.get("history")
        )
        if isinstance(sub_list, list):
            wrap_id = parsed.get("id") or parsed.get("session_id") or parsed.get("sessionId")
            for item in sub_list:
                if isinstance(item, dict):
                    copied = dict(item)
                    if "session_id" not in copied and wrap_id:
                        copied["session_id"] = wrap_id
                    entries.append(copied)
        else:
            entries.append(parsed)
    if not entries and content.strip():
        try:
            full_parsed = json.loads(content)
        except ValueError:
            full_parsed = None
        if isinstance(full_parsed, list):
            entries = [item for item in full_parsed if isinstance(item, dict)]
        elif isinstance(full_parsed, dict):
            file_top_id = str(
                full_parsed.get("id") or full_parsed.get("session_id") or full_parsed.get("sessionId") or ""
            )
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
                        copied = dict(item)
                        if "session_id" not in copied and file_top_id:
                            copied["session_id"] = file_top_id
                        entries.append(copied)
    return entries, file_top_id


def iter_codex_sessions(home: Path):
    codex_dir = home / ".codex"
    if not inventory._is_dir(codex_dir):
        return
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
        return

    sessions: dict[str, Session] = {}
    firsts: dict[str, str] = {}

    for path in session_files:
        try:
            entries, file_top_id = _read_codex_entries(path)
        except OSError:
            continue
        if not entries:
            continue

        file_sid = file_top_id
        if not file_sid:
            for entry in entries:
                etype = entry.get("type")
                p = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
                if etype in ("session_meta", "session", "session_start"):
                    cand = (
                        p.get("id")
                        or p.get("session_id")
                        or p.get("sessionId")
                        or entry.get("id")
                        or entry.get("session_id")
                        or entry.get("sessionId")
                    )
                else:
                    cand = (
                        p.get("session_id")
                        or p.get("sessionId")
                        or entry.get("session_id")
                        or entry.get("sessionId")
                    )
                if cand:
                    file_sid = str(cand)
                    break

        sid = file_sid or path.stem

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

            ts = (
                detect._epoch_to_iso(entry.get("timestamp"))
                or detect._epoch_to_iso(payload.get("timestamp"))
                or (detect._epoch_to_iso(msg_dict.get("timestamp")) if msg_dict else None)
            )
            if ts and sid not in firsts:
                firsts[sid] = ts

            if etype in ("session_meta", "session", "session_start"):
                if ts:
                    sessions.setdefault(sid, Session("codex", sid, None))
                continue

            if ts:
                sessions.setdefault(sid, Session("codex", sid, None))

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
                for text in _user_texts(raw_content):
                    if detect._CODEX_HEADER_RE.search(text):
                        text = detect._CODEX_HEADER_RE.sub("", text, count=1).strip()
                    elif "## My request for Codex:" in text:
                        text = text.split("## My request for Codex:", 1)[1].strip()
                    text = text.strip()
                    if "/" in text or "<command-name>" in text.lower():
                        sessions.setdefault(sid, Session("codex", sid, None)).calls.append(prompt_call(text, ts))

            tool_calls_to_process: list[tuple[str, dict]] = []
            if etype == "response_item" and p_type in (
                "function_call",
                "tool_call",
                "custom_tool_call",
                "tool_use",
                "call",
                "tool",
            ):
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
                    if part.get("type") in ("tool_use", "tool_call", "function_call", "toolCall", "call"):
                        tc = _extract_codex_tool_call(part)
                        if tc:
                            tool_calls_to_process.append(tc)

            seen_calls: set[tuple[str, str]] = set()
            for tool_name, tool_input in tool_calls_to_process:
                call_key = (tool_name, json.dumps(tool_input, sort_keys=True))
                if call_key in seen_calls:
                    continue
                seen_calls.add(call_key)
                sessions.setdefault(sid, Session("codex", sid, None)).calls.append(ToolCall(ts, tool_name, tool_input))

    for sid, session in sessions.items():
        session.ts = _session_ts(firsts.get(sid))
        yield session


def collect_sessions(home: Path, agent: str | None = None):
    if agent in (None, "all", "antigravity"):
        yield from iter_antigravity_sessions(home)
    if agent in (None, "all", "claude"):
        yield from iter_claude_sessions(home)
    if agent in (None, "all", "pi"):
        yield from iter_pi_sessions(home)
    if agent in (None, "all", "hermes"):
        yield from iter_hermes_sessions(home)
    if agent in (None, "all", "codex"):
        yield from iter_codex_sessions(home)


# ---------------------------------------------------------------------------
# Rendering & CSV export


def row_status(
    row: SkillFollow,
    min_sessions: int = DEFAULT_MIN_SESSIONS,
    low_rate: float = DEFAULT_LOW_RATE,
) -> str:
    if row.sessions_scored == 0:
        return "no-support-files" if row.matched else "unmatched"
    if row.sessions_scored < min_sessions:
        return "insufficient-sessions"
    return "low-followthrough" if (row.rate is not None and row.rate < low_rate) else "ok"


def render(rows, min_sessions: int = DEFAULT_MIN_SESSIONS, low_rate: float = DEFAULT_LOW_RATE, days: int | None = None) -> str:
    window = f"last {days} days" if days is not None else "all history"
    out = [
        "# Skill follow-through report",
        "",
        f"- Window: {window}. Agents: {', '.join(FOLLOW_AGENTS)} transcripts.",
        "- Follow-through: after a skill loads in a session, the agent reads or runs a file in that skill's folder",
        "  or a file its SKILL.md links to (for example another skill it delegates to).",
        "- Scored only for loads of a skill copy that has such files. Low follow-through is a review signal, not proof of low quality:",
        "  the skill may have needed only its SKILL.md for that task.",
        "",
    ]
    scored = [r for r in rows if r.has_support]
    unscored = [r for r in rows if not r.has_support and r.matched]
    unmatched = [r for r in rows if not r.matched and not r.has_support]
    if scored:
        out.append("| Skill | Sessions loaded | Scored | Followed through | Rate | Read reference | Ran script |")
        out.append("|---|---|---|---|---|---|---|")
        for r in scored:
            out.append(
                f"| {r.name} | {r.sessions_loaded} | {r.sessions_scored} | {r.sessions_followed} | {r.rate:.0%} "
                f"| {r.kinds['reference']} | {r.kinds['script']} |"
            )
    else:
        out.append("No loads of skills with support files found.")

    low = [r for r in scored if r.sessions_scored >= min_sessions and r.rate < low_rate]
    out += ["", f"## Low follow-through (rate below {low_rate:.0%}, at least {min_sessions} sessions)", ""]
    out += [f"- {r.name}: {r.sessions_followed} of {r.sessions_scored} sessions" for r in low] or ["None."]
    few = [r for r in scored if r.sessions_scored < min_sessions]
    if few:
        out += ["", f"{len(few)} scored skill(s) have fewer than {min_sessions} sessions; their rates are not flagged."]

    out += ["", "## Loaded, nothing to follow through (SKILL.md only)", ""]
    out += [f"- {r.name}: {r.sessions_loaded} sessions" for r in unscored] or ["None."]
    if unmatched:
        out += ["", "## Loaded, not in current inventory (not scored)", ""]
        out += [f"- {r.name}: {r.sessions_loaded} sessions" for r in unmatched]
    return "\n".join(out) + "\n"


def write_csv(
    rows,
    path,
    min_sessions: int = DEFAULT_MIN_SESSIONS,
    low_rate: float = DEFAULT_LOW_RATE,
) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "skill",
                "sessions_loaded",
                "sessions_scored",
                "sessions_followed",
                "rate",
                "read_reference",
                "ran_script",
                "status",
            ]
        )
        for r in rows:
            writer.writerow(
                [
                    r.name,
                    r.sessions_loaded,
                    r.sessions_scored,
                    r.sessions_followed,
                    round(r.rate, 4) if r.rate is not None else "",
                    r.kinds["reference"],
                    r.kinds["script"],
                    row_status(r, min_sessions=min_sessions, low_rate=low_rate),
                ]
            )
