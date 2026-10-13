import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from tracker import detect, inventory, store

_POST_TOOL_USE = re.compile(rb'"hook_event_name"\s*:\s*"PostToolUse"')
_SKILL_HINT = re.compile(rb"skill", re.IGNORECASE)


def _worth_parsing(raw):
    # Ordinary tool calls never mention a skill, so skip decoding their (possibly huge) payloads.
    return _POST_TOOL_USE.search(raw) is None or _SKILL_HINT.search(raw) is not None


def _log_error(base, exc):
    try:
        base.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).isoformat()
        with open(base / "hook-errors.log", "a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {type(exc).__name__}\n")
    except OSError:
        pass


def run(raw, data_dir=None, home=None):
    try:
        base = data_dir or store.default_data_dir()
    except Exception:
        return
    try:
        if not _worth_parsing(raw):
            return
        event = json.loads(raw.decode("utf-8-sig"))
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
    try:
        run(sys.stdin.buffer.read())
    except BaseException:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
