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


def default_evals_dir() -> Path:
    override = os.environ.get("SKILL_USAGE_EVALS")
    if override:
        return Path(override)
    return default_data_dir().parent / "evals"


def _append_jsonl(prefix: str, record: dict, data_dir: Path | None = None) -> None:
    base = data_dir or default_data_dir()
    base.mkdir(parents=True, exist_ok=True)
    match = _MONTH.match(str(record.get("ts", "")))
    month = match.group(0) if match else datetime.now(timezone.utc).strftime("%Y-%m")
    with open(base / f"{prefix}-{month}.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, separators=(",", ":")) + "\n")


def _read_jsonl_glob(pattern: str, data_dir: Path | None = None) -> list[dict]:
    base = data_dir or default_data_dir()
    records: list[dict] = []
    if not base.is_dir():
        return records
    for path in sorted(base.glob(pattern)):
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    records.append(obj)
    return records


def append_event(record: dict, data_dir: Path | None = None) -> None:
    _append_jsonl("events", record, data_dir=data_dir)


def read_events(data_dir: Path | None = None) -> list[dict]:
    return _read_jsonl_glob("events-*.jsonl", data_dir=data_dir)


def append_eval(record: dict, data_dir: Path | None = None) -> None:
    _append_jsonl("evals", record, data_dir=data_dir)


def read_evals(data_dir: Path | None = None) -> list[dict]:
    return _read_jsonl_glob("evals-*.jsonl", data_dir=data_dir)


def latest_evals_by_skill(records: list[dict], agent: str | None = None) -> dict[str, dict]:
    """Return the most recent eval record per lowercase skill name."""
    latest: dict[str, dict] = {}
    for rec in records:
        if rec.get("type") not in (None, "eval"):
            continue
        skill = rec.get("skill")
        if not isinstance(skill, str) or not skill:
            continue
        if agent is not None and agent != "all":
            rec_agent = rec.get("agent")
            if rec_agent and rec_agent != agent:
                continue
        key = skill.lower()
        prev = latest.get(key)
        if prev is None or str(rec.get("ts", "")) >= str(prev.get("ts", "")):
            latest[key] = rec
    return latest
