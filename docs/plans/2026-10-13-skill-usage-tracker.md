# Copilot Skill Usage Tracker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record which skills GitHub Copilot loads in VS Code Local sessions and report skills with no observed loads, entirely locally.

**Architecture:** A VS Code Local user-level hook runs `hook.py` on `SessionStart`, `UserPromptSubmit`, and `PostToolUse`. `tracker.detect` turns each hook payload into a metadata-only record, and `tracker.store` appends it to a monthly JSONL file. `skill_usage.py report` joins the events with a filesystem inventory of skill copies and prints a Markdown report with an optional CSV.

**Tech Stack:** Python 3.12 standard library only (`json`, `os`, `pathlib`, `argparse`, `csv`, `unittest`).

**Spec:** [design.md](../design.md). Terms: [GLOSSARY.md](../../GLOSSARY.md).

**Conventions:**
- Project root: `C:\Users\uie83312\.copilot\skill-usage` (all paths below are relative to it unless absolute).
- Run commands from the project root in PowerShell.
- Test command: `py -3.12 -m unittest discover -s tests -t . -v`
- Interpreter for the hook: `C:\Users\uie83312\AppData\Local\Programs\Python\Python312\python.exe` (no spaces in the path, so the hook command needs no quoting).

## File Structure

| File | Responsibility |
|---|---|
| `tracker/__init__.py` | Package marker |
| `tracker/detect.py` | Pure function: hook payload to a record (`load` or `session`) or `None` |
| `tracker/store.py` | Append and read monthly JSONL event files |
| `tracker/inventory.py` | Scan skill copies from project, personal, and plugin locations |
| `tracker/report.py` | Summarize events plus inventory; render Markdown; write CSV |
| `hook.py` | Hook entry point; never raises, never writes to stdout |
| `skill_usage.py` | CLI: `report` subcommand |
| `tests/` | `unittest` tests, one file per module |
| `tools/capture_hook.py` | Temporary payload capture (deleted after Task 1) |
| `README.md` | Install, disable, uninstall, usage |

---

### Task 1: Scaffold, capture real payloads, amend design

**Files:**
- Create: `.gitignore`, `tests/__init__.py`, `tracker/__init__.py`, `tools/capture_hook.py`
- Create (temporary): `C:\Users\uie83312\.copilot\hooks\skill-usage-capture.json`
- Modify: `docs/design.md`

- [ ] **Step 1: Initialize the project**

```powershell
Set-Location $HOME\.copilot\skill-usage
git init
New-Item -ItemType Directory -Force tests, tracker, tools, data, capture | Out-Null
Set-Content -Path .gitignore -Value "data/`ncapture/`n__pycache__/"
Set-Content -Path tests\__init__.py -Value ""
Set-Content -Path tracker\__init__.py -Value '"""Local Copilot skill usage tracker."""'
git add -A
git commit -m "chore: scaffold skill usage tracker"
```

Expected: `git commit` reports the new files.

- [ ] **Step 2: Create the temporary capture hook**

Create `tools/capture_hook.py`:

```python
import json
import sys
from pathlib import Path

OUT = Path.home() / ".copilot" / "skill-usage" / "capture" / "payloads.jsonl"


def short(value):
    if isinstance(value, str):
        return value[:200]
    if isinstance(value, dict):
        return {k: short(v) for k, v in value.items()}
    if isinstance(value, list):
        return [short(v) for v in value[:10]]
    return value


event = json.loads(sys.stdin.buffer.read().decode("utf-8"))
kept = {k: short(v) for k, v in event.items() if k != "tool_response"}
OUT.parent.mkdir(parents=True, exist_ok=True)
with open(OUT, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(kept) + "\n")
```

Create `C:\Users\uie83312\.copilot\hooks\skill-usage-capture.json`:

```json
{
  "hooks": {
    "SessionStart": [
      {"type": "command", "command": "C:\\Users\\uie83312\\AppData\\Local\\Programs\\Python\\Python312\\python.exe C:\\Users\\uie83312\\.copilot\\skill-usage\\tools\\capture_hook.py", "timeout": 10}
    ],
    "UserPromptSubmit": [
      {"type": "command", "command": "C:\\Users\\uie83312\\AppData\\Local\\Programs\\Python\\Python312\\python.exe C:\\Users\\uie83312\\.copilot\\skill-usage\\tools\\capture_hook.py", "timeout": 10}
    ],
    "PostToolUse": [
      {"type": "command", "command": "C:\\Users\\uie83312\\AppData\\Local\\Programs\\Python\\Python312\\python.exe C:\\Users\\uie83312\\.copilot\\skill-usage\\tools\\capture_hook.py", "timeout": 10}
    ]
  }
}
```

The capture keeps prompts and tool inputs truncated to 200 characters and drops tool responses. It is deleted in Step 5.

- [ ] **Step 3: Capture payloads in a disposable Local session (user action)**

In VS Code, set the Session Target to **Local**, open a new chat, and send these prompts in order:
1. `read the file C:\Users\uie83312\.copilot\skills\graphify\SKILL.md and say only "done"`
2. `/grill-me say only "done"`
3. If the Skill tool is enabled (`github.copilot.chat.skillTool.enabled`): `use the brainstorming skill for a one-line idea, say only "done"`

Check the **GitHub Copilot Chat Hooks** output channel if `capture\payloads.jsonl` does not appear.

- [ ] **Step 4: Inspect the capture**

```powershell
Get-Content $HOME\.copilot\skill-usage\capture\payloads.jsonl
```

Record these four facts (they feed Task 2):
1. The `tool_name` and the path key inside `tool_input` for the `SKILL.md` read.
2. Whether a Skill-tool event exists, and its `tool_name` and the key holding the skill name.
3. The exact `prompt` text for the slash command (including any plugin prefix).
4. That `SessionStart` appears once per session and carries `session_id` and `cwd`.

- [ ] **Step 5: Remove the capture hook**

```powershell
Remove-Item $HOME\.copilot\hooks\skill-usage-capture.json
Remove-Item -Recurse -Force $HOME\.copilot\skill-usage\capture
Remove-Item $HOME\.copilot\skill-usage\tools\capture_hook.py
```

- [ ] **Step 6: Amend the design for the session event**

The 20-session threshold needs sessions with no loads to be counted, so the hook also records a `session` event on `SessionStart`. In `docs/design.md`, in "Architecture and data flow", change the first bullet to:

```
- A hook configuration under `~/.copilot/hooks/` runs a small recorder on `SessionStart`, `UserPromptSubmit`, and `PostToolUse`. `SessionStart` records a `session` event (timestamp, session ID, workspace folder name) so the report can count sessions that loaded no skill.
```

Then in "Open items", delete the two items that Step 4 resolved (tool shapes and slash prompt text).

- [ ] **Step 7: Commit**

```powershell
git add -A
git commit -m "docs: record session events in design"
```

---

### Task 2: Detect loads from hook payloads

**Files:**
- Create: `tracker/detect.py`
- Test: `tests/test_detect.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_detect.py`:

```python
import json
import os
import unittest

from tracker.detect import detect

SKILL = os.path.join("home", "u", ".copilot", "skills", "graphify", "SKILL.md")
CWD = os.path.join("work", "mc_sw")
TS = "2026-10-13T08:00:00.000Z"


def tool_event(tool, **tool_input):
    return {
        "hook_event_name": "PostToolUse",
        "timestamp": TS,
        "session_id": "s1",
        "cwd": CWD,
        "tool_name": tool,
        "tool_input": tool_input,
        "tool_response": "secret text",
    }


def prompt_event(prompt):
    return {
        "hook_event_name": "UserPromptSubmit",
        "timestamp": TS,
        "session_id": "s1",
        "cwd": CWD,
        "prompt": prompt,
    }


def no_names():
    return set()


class DetectTests(unittest.TestCase):
    def test_agent_read_of_skill_file(self):
        rec = detect(tool_event("read_file", filePath=SKILL, startLine=1, endLine=50), no_names)
        self.assertEqual(rec["type"], "load")
        self.assertEqual(rec["skill"], "graphify")
        self.assertEqual(rec["source"], "agent-read")
        self.assertEqual(rec["path"], SKILL)
        self.assertEqual(rec["session_id"], "s1")
        self.assertEqual(rec["workspace"], "mc_sw")
        self.assertEqual(rec["ts"], TS)

    def test_ignores_reads_that_are_not_skill_files(self):
        self.assertIsNone(detect(tool_event("read_file", filePath=os.path.join("work", "README.md")), no_names))
        self.assertIsNone(detect(tool_event("read_file", filePath=os.path.join("work", "docs", "SKILL.md")), no_names))

    def test_ignores_edits_of_skill_file(self):
        self.assertIsNone(detect(tool_event("replace_string_in_file", filePath=SKILL), no_names))

    def test_skill_tool_with_plugin_prefix(self):
        rec = detect(tool_event("Skill", skill="superpowers:brainstorming"), no_names)
        self.assertEqual(rec["skill"], "brainstorming")
        self.assertEqual(rec["source"], "skill-tool")
        self.assertEqual(rec["path"], "")

    def test_slash_command_for_known_skill(self):
        rec = detect(prompt_event("/grill-me plan the thing"), lambda: {"grill-me"})
        self.assertEqual(rec["skill"], "grill-me")
        self.assertEqual(rec["source"], "user-slash")

    def test_plugin_prefixed_slash_command(self):
        rec = detect(prompt_event("/mattpocock:tdd now"), lambda: {"tdd"})
        self.assertEqual(rec["skill"], "tdd")

    def test_unknown_or_non_leading_slash_is_ignored(self):
        self.assertIsNone(detect(prompt_event("/clear"), lambda: {"tdd"}))
        self.assertIsNone(detect(prompt_event("hello /tdd"), lambda: {"tdd"}))

    def test_inventory_not_scanned_for_plain_prompts(self):
        def boom():
            raise AssertionError("inventory scanned")

        self.assertIsNone(detect(prompt_event("just a question"), boom))

    def test_session_start_event(self):
        event = {"hook_event_name": "SessionStart", "timestamp": TS, "session_id": "s1", "cwd": CWD, "source": "new"}
        rec = detect(event, no_names)
        self.assertEqual(rec, {"type": "session", "ts": TS, "session_id": "s1", "workspace": "mc_sw"})

    def test_record_never_contains_prompt_or_response(self):
        text = json.dumps(detect(prompt_event("/tdd topsecret"), lambda: {"tdd"}))
        self.assertNotIn("topsecret", text)
        text = json.dumps(detect(tool_event("read_file", filePath=SKILL), no_names))
        self.assertNotIn("secret text", text)

    def test_malformed_input_is_ignored(self):
        self.assertIsNone(detect({}, no_names))
        bad = {"hook_event_name": "PostToolUse", "tool_name": "read_file", "tool_input": "x"}
        self.assertIsNone(detect(bad, no_names))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `py -3.12 -m unittest discover -s tests -t . -v`
Expected: FAIL/ERROR with `ModuleNotFoundError: No module named 'tracker.detect'`.

- [ ] **Step 3: Write the implementation**

Create `tracker/detect.py`. The constants below are the defaults; if the Task 1 capture showed a different read `tool_name`, path key, Skill-tool name or key, or slash prompt format, change `READ_TOOL_NAMES`, `PATH_KEYS`, `SKILL_TOOL_NAMES`, `SKILL_NAME_KEYS`, or `SLASH_RE` to match and add a test that uses the captured value.

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `py -3.12 -m unittest discover -s tests -t . -v`
Expected: all `DetectTests` PASS, final line `OK`.

- [ ] **Step 5: Commit**

```powershell
git add tests/test_detect.py tracker/detect.py
git commit -m "feat: detect skill loads from hook payloads"
```

---

### Task 3: Event store

**Files:**
- Create: `tracker/store.py`
- Test: `tests/test_store.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_store.py`:

```python
import tempfile
import unittest
from pathlib import Path

from tracker import store


class StoreTests(unittest.TestCase):
    def test_events_go_to_monthly_files_and_read_back_in_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store.append_event({"type": "load", "ts": "2026-09-30T23:59:00Z", "skill": "a"}, base)
            store.append_event({"type": "load", "ts": "2026-10-01T00:01:00Z", "skill": "b"}, base)
            self.assertTrue((base / "events-2026-09.jsonl").is_file())
            self.assertTrue((base / "events-2026-10.jsonl").is_file())
            self.assertEqual([e["skill"] for e in store.read_events(base)], ["a", "b"])

    def test_malformed_and_non_object_lines_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "events-2026-10.jsonl").write_text(
                'not json\n{"type":"session","ts":"2026-10-01T00:00:00Z"}\n[1]\n', encoding="utf-8"
            )
            events = store.read_events(base)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["type"], "session")

    def test_missing_directory_reads_as_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(store.read_events(Path(tmp) / "nope"), [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `py -3.12 -m unittest tests.test_store -v`
Expected: ERROR `ImportError: cannot import name 'store' from 'tracker'`.

- [ ] **Step 3: Write the implementation**

Create `tracker/store.py`:

```python
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

_MONTH = re.compile(r"^\d{4}-\d{2}")


def default_data_dir() -> Path:
    override = os.environ.get("SKILL_USAGE_DATA")
    if override:
        return Path(override)
    return Path.home() / ".copilot" / "skill-usage" / "data"


def append_event(record: dict, data_dir: Path | None = None) -> None:
    base = data_dir or default_data_dir()
    base.mkdir(parents=True, exist_ok=True)
    match = _MONTH.match(str(record.get("ts", "")))
    month = match.group(0) if match else datetime.now(timezone.utc).strftime("%Y-%m")
    with open(base / f"events-{month}.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, separators=(",", ":")) + "\n")


def read_events(data_dir: Path | None = None) -> list[dict]:
    base = data_dir or default_data_dir()
    events: list[dict] = []
    if not base.is_dir():
        return events
    for path in sorted(base.glob("events-*.jsonl")):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    events.append(obj)
    return events
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `py -3.12 -m unittest discover -s tests -t . -v`
Expected: `OK`.

- [ ] **Step 5: Commit**

```powershell
git add tests/test_store.py tracker/store.py
git commit -m "feat: add monthly event store"
```

---

### Task 4: Skill inventory

**Files:**
- Create: `tracker/inventory.py`
- Test: `tests/test_inventory.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_inventory.py`:

```python
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


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `py -3.12 -m unittest tests.test_inventory -v`
Expected: ERROR `ImportError: cannot import name 'inventory' from 'tracker'`.

- [ ] **Step 3: Write the implementation**

Create `tracker/inventory.py`:

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `py -3.12 -m unittest discover -s tests -t . -v`
Expected: `OK`.

- [ ] **Step 5: Commit**

```powershell
git add tests/test_inventory.py tracker/inventory.py
git commit -m "feat: inventory skill copies from project, personal, and plugin locations"
```

---

### Task 5: Hook entry point

**Files:**
- Create: `hook.py`
- Test: `tests/test_hook.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_hook.py`:

```python
import json
import tempfile
import unittest
from pathlib import Path

import hook
from tracker import store

TS = "2026-10-13T08:00:00.000Z"


def make_skill(root: Path, *parts):
    folder = root.joinpath(*parts)
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("x", encoding="utf-8")
    return folder / "SKILL.md"


def payload(**fields):
    base = {"timestamp": TS, "session_id": "s1", "cwd": str(Path("work") / "mc_sw")}
    base.update(fields)
    return json.dumps(base).encode("utf-8")


class HookTests(unittest.TestCase):
    def test_records_agent_read_of_skill_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "data"
            skill = make_skill(Path(tmp), "home", ".copilot", "skills", "graphify")
            raw = payload(
                hook_event_name="PostToolUse",
                tool_name="read_file",
                tool_input={"filePath": str(skill)},
                tool_response="x",
            )
            hook.run(raw, data_dir=base, home=Path(tmp) / "home")
            events = store.read_events(base)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["skill"], "graphify")
            self.assertEqual(events[0]["source"], "agent-read")

    def test_records_slash_command_using_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "data"
            make_skill(Path(tmp), "home", ".agents", "skills", "tdd")
            hook.run(
                payload(hook_event_name="UserPromptSubmit", prompt="/tdd go"),
                data_dir=base,
                home=Path(tmp) / "home",
            )
            events = store.read_events(base)
            self.assertEqual([(e["skill"], e["source"]) for e in events], [("tdd", "user-slash")])

    def test_records_session_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "data"
            hook.run(payload(hook_event_name="SessionStart", source="new"), data_dir=base, home=Path(tmp))
            self.assertEqual([e["type"] for e in store.read_events(base)], ["session"])

    def test_unrelated_tool_calls_record_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "data"
            raw = payload(hook_event_name="PostToolUse", tool_name="run_in_terminal", tool_input={"command": "ls"})
            hook.run(raw, data_dir=base, home=Path(tmp))
            self.assertEqual(store.read_events(base), [])

    def test_bad_input_logs_only_the_error_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "data"
            hook.run(b"not json {topsecret}", data_dir=base, home=Path(tmp))
            self.assertEqual(store.read_events(base), [])
            log = (base / "hook-errors.log").read_text(encoding="utf-8")
            self.assertIn("JSONDecodeError", log)
            self.assertNotIn("topsecret", log)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `py -3.12 -m unittest tests.test_hook -v`
Expected: ERROR `ModuleNotFoundError: No module named 'hook'`.

- [ ] **Step 3: Write the implementation**

Create `hook.py`:

```python
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from tracker import detect, inventory, store


def _log_error(base, exc):
    try:
        base.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).isoformat()
        with open(base / "hook-errors.log", "a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {type(exc).__name__}\n")
    except OSError:
        pass


def run(raw, data_dir=None, home=None):
    base = data_dir or store.default_data_dir()
    try:
        event = json.loads(raw.decode("utf-8"))
        if not isinstance(event, dict):
            return
        cwd = event.get("cwd")
        workspaces = [Path(cwd)] if isinstance(cwd, str) and cwd else []

        def names():
            return inventory.known_names(home or Path.home(), workspaces)

        record = detect.detect(event, names)
        if record:
            store.append_event(record, base)
    except Exception as exc:
        _log_error(base, exc)


def main():
    run(sys.stdin.buffer.read())
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `py -3.12 -m unittest discover -s tests -t . -v`
Expected: `OK`.

- [ ] **Step 5: Commit**

```powershell
git add hook.py tests/test_hook.py
git commit -m "feat: add non-blocking hook recorder"
```

---

### Task 6: Report

**Files:**
- Create: `tracker/report.py`
- Test: `tests/test_report.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_report.py`:

```python
import csv
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tracker import report
from tracker.inventory import SkillCopy

NOW = datetime(2026, 11, 20, tzinfo=timezone.utc)

COPIES = [
    SkillCopy("used", "/p/used/SKILL.md", "personal"),
    SkillCopy("idle", "/p/idle/SKILL.md", "personal"),
    SkillCopy("dup", "/a/dup/SKILL.md", "personal"),
    SkillCopy("dup", "/b/dup/SKILL.md", "plugin:m/p"),
]


def ts(days_ago):
    return (NOW - timedelta(days=days_ago)).isoformat()


def load(skill, source, sid, days_ago):
    return {
        "type": "load",
        "ts": ts(days_ago),
        "session_id": sid,
        "workspace": "w",
        "skill": skill,
        "path": "",
        "source": source,
    }


def session(sid, days_ago):
    return {"type": "session", "ts": ts(days_ago), "session_id": sid, "workspace": "w"}


def rows_by_name(summary):
    return {row.name: row for row in summary.rows}


def enough_events():
    events = [session("old", 40)]
    events += [session(f"s{i}", 1 + i) for i in range(25)]
    events += [load("used", "agent-read", "s1", 2), load("ghost", "skill-tool", "s2", 3)]
    return events


class ReportTests(unittest.TestCase):
    def test_counts_by_source_with_sessions_and_last_load(self):
        events = [
            load("used", "agent-read", "s1", 5),
            load("used", "agent-read", "s1", 4),
            load("used", "user-slash", "s2", 6),
        ]
        row = rows_by_name(report.summarize(events, COPIES, NOW, 30))["used"]
        self.assertEqual(row.counts, {"agent-read": 2, "user-slash": 1, "skill-tool": 0})
        self.assertEqual(row.total, 3)
        self.assertEqual(row.sessions, {"s1", "s2"})
        self.assertEqual(row.last, NOW - timedelta(days=4))

    def test_insufficient_data_is_not_called_review_candidate(self):
        events = [session("s1", 3), load("used", "agent-read", "s1", 2)]
        summary = report.summarize(events, COPIES, NOW, 30)
        text = report.render(summary, 30, 20)
        self.assertIn("Insufficient data", text)
        self.assertIn("## Not observed (insufficient data)", text)
        self.assertNotIn("Review candidates", text)
        self.assertIn("idle", text)

    def test_enough_coverage_lists_review_candidates(self):
        summary = report.summarize(enough_events(), COPIES, NOW, 30)
        self.assertTrue(report.meets_threshold(summary, 30, 20))
        text = report.render(summary, 30, 20)
        self.assertIn("## Review candidates (not observed)", text)
        self.assertIn("- idle (personal)", text)

    def test_short_window_never_meets_threshold(self):
        summary = report.summarize(enough_events(), COPIES, NOW, 7)
        self.assertFalse(report.meets_threshold(summary, 30, 20))

    def test_duplicate_names_list_every_copy(self):
        summary = report.summarize(enough_events(), COPIES, NOW, 30)
        text = report.render(summary, 30, 20)
        self.assertIn("## Duplicate names", text)
        self.assertIn("/a/dup/SKILL.md (personal)", text)
        self.assertIn("/b/dup/SKILL.md (plugin:m/p)", text)

    def test_loads_without_inventory_entry_are_unmatched(self):
        summary = report.summarize(enough_events(), COPIES, NOW, 30)
        self.assertEqual(rows_by_name(summary)["ghost"].copies, [])
        self.assertIn("- ghost: 1 loads", report.render(summary, 30, 20))

    def test_csv_status_values(self):
        summary = report.summarize(enough_events(), COPIES, NOW, 30)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "r.csv"
            report.write_csv(summary, out, True)
            with open(out, newline="", encoding="utf-8") as fh:
                status = {row["skill"]: row["status"] for row in csv.DictReader(fh)}
        self.assertEqual(
            status,
            {"used": "loaded", "idle": "review-candidate", "dup": "review-candidate", "ghost": "unmatched"},
        )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `py -3.12 -m unittest tests.test_report -v`
Expected: ERROR `ImportError: cannot import name 'report' from 'tracker'`.

- [ ] **Step 3: Write the implementation**

Create `tracker/report.py`:

```python
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

SOURCES = ("agent-read", "user-slash", "skill-tool")


def parse_ts(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass
class Row:
    name: str
    copies: list = field(default_factory=list)
    counts: dict = field(default_factory=lambda: {s: 0 for s in SOURCES})
    sessions: set = field(default_factory=set)
    last: datetime | None = None

    @property
    def total(self):
        return sum(self.counts.values())


@dataclass
class Summary:
    days: int
    first: datetime | None
    coverage_days: int
    window_days: int
    sessions: set
    rows: list


def summarize(events, copies, now, days):
    timed = []
    for event in events:
        stamp = parse_ts(event.get("ts"))
        if stamp is not None:
            timed.append((stamp, event))
    first = min((t for t, _ in timed), default=None)
    coverage_days = (now - first).days if first else 0
    cutoff = now - timedelta(days=days)
    in_window = [(t, e) for t, e in timed if t >= cutoff]
    sessions = {e["session_id"] for _, e in in_window if e.get("session_id")}

    by_name: dict[str, Row] = {}
    for copy in copies:
        by_name.setdefault(copy.name, Row(copy.name)).copies.append(copy)
    for stamp, event in in_window:
        name = event.get("skill")
        source = event.get("source")
        if event.get("type") != "load" or not name or source not in SOURCES:
            continue
        row = by_name.setdefault(name, Row(name))
        row.counts[source] += 1
        if event.get("session_id"):
            row.sessions.add(event["session_id"])
        if row.last is None or stamp > row.last:
            row.last = stamp

    rows = sorted(by_name.values(), key=lambda r: (-r.total, r.name))
    return Summary(days, first, coverage_days, min(days, coverage_days), sessions, rows)


def meets_threshold(summary, min_days, min_sessions):
    return summary.window_days >= min_days and len(summary.sessions) >= min_sessions


def _sources(row):
    return ", ".join(sorted({c.source for c in row.copies}))


def render(summary, min_days, min_sessions):
    enough = meets_threshold(summary, min_days, min_sessions)
    started = summary.first.date().isoformat() if summary.first else "no events recorded yet"
    out = [
        "# Copilot skill load report",
        "",
        f"- Window: last {summary.days} days (tracking started: {started})",
        f"- Coverage in window: {summary.window_days} days, {len(summary.sessions)} sessions",
        "- Covers only sessions recorded after the hook was installed; inventory reflects today, not past availability.",
    ]
    if enough:
        out.append(f"- Coverage meets the review threshold ({min_days} days, {min_sessions} sessions).")
    else:
        out.append(
            f"- Insufficient data for review candidates: {summary.window_days} of {min_days} days, "
            f"{len(summary.sessions)} of {min_sessions} sessions."
        )

    out += ["", "## Loads by skill", ""]
    loaded = [r for r in summary.rows if r.total]
    if loaded:
        out.append("| Skill | Copies | agent-read | user-slash | skill-tool | Total | Sessions | Last load |")
        out.append("|---|---|---|---|---|---|---|---|")
        for r in loaded:
            c = r.counts
            out.append(
                f"| {r.name} | {len(r.copies)} | {c['agent-read']} | {c['user-slash']} | {c['skill-tool']} "
                f"| {r.total} | {len(r.sessions)} | {r.last.date().isoformat()} |"
            )
    else:
        out.append("No loads recorded in this window.")

    idle = [r for r in summary.rows if not r.total and r.copies]
    heading = "## Review candidates (not observed)" if enough else "## Not observed (insufficient data)"
    out += ["", heading, ""]
    out += [f"- {r.name} ({_sources(r)})" for r in idle] or ["None."]

    dupes = [r for r in summary.rows if len(r.copies) > 1]
    out += ["", "## Duplicate names", ""]
    for r in dupes:
        out.append(f"- {r.name}")
        out += [f"  - {c.path} ({c.source})" for c in r.copies]
    if not dupes:
        out.append("None.")

    unmatched = [r for r in summary.rows if r.total and not r.copies]
    out += ["", "## Unmatched loads (no inventory entry)", ""]
    out += [f"- {r.name}: {r.total} loads" for r in unmatched] or ["None."]
    return "\n".join(out) + "\n"


def write_csv(summary, path, enough):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["skill", "copies", "sources", "agent_read", "user_slash", "skill_tool", "total_loads", "sessions", "last_load", "status"]
        )
        for r in summary.rows:
            if r.total:
                status = "loaded" if r.copies else "unmatched"
            else:
                status = "review-candidate" if enough else "not-observed"
            writer.writerow(
                [
                    r.name,
                    len(r.copies),
                    ";".join(sorted({c.source for c in r.copies})),
                    r.counts["agent-read"],
                    r.counts["user-slash"],
                    r.counts["skill-tool"],
                    r.total,
                    len(r.sessions),
                    r.last.isoformat() if r.last else "",
                    status,
                ]
            )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `py -3.12 -m unittest discover -s tests -t . -v`
Expected: `OK`.

- [ ] **Step 5: Commit**

```powershell
git add tests/test_report.py tracker/report.py
git commit -m "feat: add Markdown and CSV report"
```

---

### Task 7: Report CLI

**Files:**
- Create: `skill_usage.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_cli.py`:

```python
import contextlib
import io
import tempfile
import unittest
from pathlib import Path

import skill_usage
from tracker import store


class CliTests(unittest.TestCase):
    def test_report_prints_markdown_and_writes_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "data"
            store.append_event(
                {"type": "session", "ts": "2026-10-13T08:00:00Z", "session_id": "s1", "workspace": "w"}, base
            )
            csv_path = Path(tmp) / "out.csv"
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = skill_usage.main(
                    ["report", "--data-dir", str(base), "--workspace", tmp, "--csv", str(csv_path)]
                )
            self.assertEqual(code, 0)
            self.assertIn("# Copilot skill load report", buffer.getvalue())
            self.assertTrue(csv_path.is_file())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `py -3.12 -m unittest tests.test_cli -v`
Expected: ERROR `ModuleNotFoundError: No module named 'skill_usage'`.

- [ ] **Step 3: Write the implementation**

Create `skill_usage.py`:

```python
import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from tracker import inventory, report, store


def main(argv=None):
    parser = argparse.ArgumentParser(prog="skill_usage")
    sub = parser.add_subparsers(dest="command", required=True)
    rep = sub.add_parser("report", help="print a skill load report")
    rep.add_argument("--days", type=int, default=30)
    rep.add_argument("--min-days", type=int, default=30)
    rep.add_argument("--min-sessions", type=int, default=20)
    rep.add_argument("--workspace", action="append", default=[], type=Path)
    rep.add_argument("--csv", type=Path)
    rep.add_argument("--data-dir", type=Path)
    args = parser.parse_args(argv)

    workspaces = args.workspace or [Path.cwd()]
    events = store.read_events(args.data_dir)
    copies = inventory.scan(Path.home(), workspaces)
    now = datetime.now(timezone.utc)
    summary = report.summarize(events, copies, now, args.days)
    print(report.render(summary, args.min_days, args.min_sessions))
    if args.csv:
        enough = report.meets_threshold(summary, args.min_days, args.min_sessions)
        report.write_csv(summary, args.csv, enough)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the full suite to verify it passes**

Run: `py -3.12 -m unittest discover -s tests -t . -v`
Expected: `OK`.

- [ ] **Step 5: Commit**

```powershell
git add skill_usage.py tests/test_cli.py
git commit -m "feat: add report CLI"
```

---

### Task 8: Install the hook, smoke test, and document

**Files:**
- Create: `C:\Users\uie83312\.copilot\hooks\skill-usage.json`, `README.md`

- [ ] **Step 1: Smoke test the recorder directly**

```powershell
$env:SKILL_USAGE_DATA = "$env:TEMP\skill-usage-smoke"
'{"hook_event_name":"SessionStart","timestamp":"2026-10-13T08:00:00Z","session_id":"smoke","cwd":"D:\\projects\\pkg\\mc_sw"}' | py -3.12 hook.py
Get-Content "$env:TEMP\skill-usage-smoke\events-2026-10.jsonl"
Remove-Item -Recurse -Force "$env:TEMP\skill-usage-smoke"
Remove-Item Env:SKILL_USAGE_DATA
```

Expected: no output from `hook.py`, one line `{"type":"session","ts":"2026-10-13T08:00:00Z","session_id":"smoke","workspace":"mc_sw"}`.

- [ ] **Step 2: Install the hook configuration**

Create `C:\Users\uie83312\.copilot\hooks\skill-usage.json`:

```json
{
  "hooks": {
    "SessionStart": [
      {"type": "command", "command": "C:\\Users\\uie83312\\AppData\\Local\\Programs\\Python\\Python312\\python.exe C:\\Users\\uie83312\\.copilot\\skill-usage\\hook.py", "timeout": 10}
    ],
    "UserPromptSubmit": [
      {"type": "command", "command": "C:\\Users\\uie83312\\AppData\\Local\\Programs\\Python\\Python312\\python.exe C:\\Users\\uie83312\\.copilot\\skill-usage\\hook.py", "timeout": 10}
    ],
    "PostToolUse": [
      {"type": "command", "command": "C:\\Users\\uie83312\\AppData\\Local\\Programs\\Python\\Python312\\python.exe C:\\Users\\uie83312\\.copilot\\skill-usage\\hook.py", "timeout": 10}
    ]
  }
}
```

- [ ] **Step 3: Verify in a new Local session (user action)**

Start a new Local chat and send `read C:\Users\uie83312\.copilot\skills\graphify\SKILL.md and say only "done"`. Then run:

```powershell
Get-Content $HOME\.copilot\skill-usage\data\events-*.jsonl
py -3.12 skill_usage.py report --days 30
```

Expected: a `session` event and a `load` event for `graphify` with `"source":"agent-read"`; the report shows `graphify` with one load and the "Insufficient data" line. If nothing appears, open the **GitHub Copilot Chat Hooks** output channel and check `data\hook-errors.log`.

- [ ] **Step 4: Write the README**

Create `README.md`:

```markdown
# Copilot Skill Usage Tracker

Local-only tracker of which skills GitHub Copilot loads in VS Code Local sessions. See [docs/design.md](docs/design.md) and [GLOSSARY.md](GLOSSARY.md).

## Use

    py -3.12 skill_usage.py report --days 30
    py -3.12 skill_usage.py report --days 90 --csv report.csv --workspace D:\projects\pkg\mc_sw

A skill is a review candidate only after at least 30 days and 20 sessions (`--min-days`, `--min-sessions`). Not observed does not mean unnecessary.

## Data

Events are stored in `data\events-YYYY-MM.jsonl` (metadata only: time, session ID, workspace folder name, skill, load source). Hook failures are logged by error type in `data\hook-errors.log`. Nothing leaves this machine and nothing is deleted automatically.

## Disable

Delete `C:\Users\uie83312\.copilot\hooks\skill-usage.json`.

## Uninstall and erase data

Delete the hook file above, then delete the `C:\Users\uie83312\.copilot\skill-usage` folder.

## Tests

    py -3.12 -m unittest discover -s tests -t . -v
```

- [ ] **Step 5: Final full test run and commit**

Run: `py -3.12 -m unittest discover -s tests -t . -v`
Expected: `OK`.

```powershell
git add README.md
git commit -m "docs: add usage, disable, and uninstall instructions"
```

---

## Self-Review

**Spec coverage:** load sources `agent-read`, `user-slash`, `skill-tool` (Task 2); session event for the session threshold (Tasks 1, 2); monthly files and nothing auto-deleted (Task 3); inventory of project, personal, and plugin locations with source tags, duplicates, and `.codex` excluded (Task 4); metadata-only records and non-blocking recorder (Tasks 2, 5); 30-day and 20-session review gate, "Not observed" wording, duplicates, unmatched loads, CSV (Task 6); on-demand report command (Task 7); real payload capture, install, disable, and delete instructions (Tasks 1, 8). Workspace folder name only is covered in `_record` (Task 2).

**Placeholders:** none; the only conditional is Task 2 Step 3, which names the exact constants to change if the Task 1 capture differs.

**Consistency:** `detect(event, known_names)`, `store.append_event(record, data_dir)`, `store.read_events(data_dir)`, `inventory.scan(home, workspaces)`, `inventory.known_names(home, workspaces)`, `hook.run(raw, data_dir, home)`, `report.summarize(events, copies, now, days)`, `report.render(summary, min_days, min_sessions)`, `report.meets_threshold`, and `report.write_csv(summary, path, enough)` are used identically across tasks and tests.
