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
