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
