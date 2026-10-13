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
