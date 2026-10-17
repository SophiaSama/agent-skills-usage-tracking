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


def run(raw, data_dir=None, home=None, agent=None):
    try:
        base = data_dir or store.default_data_dir()
    except Exception:
        return None
    try:
        if isinstance(raw, dict):
            event = dict(raw)
        else:
            raw_bytes = raw.encode("utf-8") if isinstance(raw, str) else raw
            if not _worth_parsing(raw_bytes):
                return None
            event = json.loads(raw_bytes.decode("utf-8-sig"))
        if not isinstance(event, dict):
            return None
        if isinstance(agent, str) and agent and not event.get("agent"):
            event["agent"] = agent
        cwd = event.get("cwd")
        workspaces = [Path(cwd)] if isinstance(cwd, str) and cwd else []
        ws_paths = event.get("workspacePaths")
        if isinstance(ws_paths, list):
            workspaces.extend(Path(p) for p in ws_paths if isinstance(p, str) and p)

        def names():
            return inventory.known_names(home or Path.home(), workspaces)

        record = detect.detect(event, names)
        if record:
            store.append_event(record, base)
        if "conversationId" in event:
            return {"decision": "allow"} if "toolCall" in event else {}
    except Exception as exc:
        _log_error(base, exc)
    return None


def _parse_agent_arg(argv):
    args = list(argv or [])
    for idx, tok in enumerate(args):
        if tok == "--agent" and idx + 1 < len(args):
            return args[idx + 1]
        if tok.startswith("--agent="):
            return tok.split("=", 1)[1]
    return None


def main(argv=None):
    try:
        agent = _parse_agent_arg(sys.argv[1:] if argv is None else argv)
        buf = getattr(sys.stdin, "buffer", None)
        if buf is not None:
            raw = buf.read()
        else:
            text = sys.stdin.read()
            raw = text.encode("utf-8") if isinstance(text, str) else text
        resp = run(raw, agent=agent)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
        elif isinstance(raw, (bytes, bytearray)) and b'"conversationId"' in raw:
            sys.stdout.write('{"decision":"allow"}\n' if b'"toolCall"' in raw else "{}\n")
    except BaseException:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
