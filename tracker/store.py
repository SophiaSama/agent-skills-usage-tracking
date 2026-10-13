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
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    events.append(obj)
    return events
