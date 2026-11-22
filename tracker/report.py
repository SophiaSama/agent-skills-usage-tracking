from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from tracker import store

SOURCES = ("agent-read", "user-slash", "skill-tool")
PAIR_SECONDS = 120
AGENT_LABELS = {
    "all": "All agents",
    "copilot": "Copilot",
    "claude": "Claude",
    "antigravity": "Antigravity",
    "pi": "Pi Agent",
    "hermes": "Hermes Agent",
    "codex": "Codex",
}


def event_agent(event: dict) -> str:
    val = event.get("agent") if isinstance(event, dict) else None
    return str(val) if isinstance(val, str) and val else "copilot"


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
    agents: set = field(default_factory=set)
    by_agent: dict = field(default_factory=dict)
    eval_summary: dict | None = None

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
    merged: int = 0
    agent: str | None = None
    by_agent: dict = field(default_factory=dict)
    has_evals: bool = False


def summarize(events, copies, now, days, agent: str | None = None, evals: list[dict] | None = None):
    return _summarize(events, copies, now, days, agent=agent, evals=evals)


def summarize_all(events, copies, now, agent: str | None = None, evals: list[dict] | None = None):
    return _summarize(events, copies, now, None, agent=agent, evals=evals)


def _summarize(events, copies, now, days, agent: str | None = None, evals: list[dict] | None = None):
    if agent is not None and agent != "all":
        events = [e for e in events if event_agent(e) == agent]
        copies = [c for c in copies if agent in getattr(c, "agents", ("copilot",))]

    timed = []
    for event in events:
        stamp = parse_ts(event.get("ts"))
        if stamp is not None:
            timed.append((stamp, event))
    first = min((t for t, _ in timed), default=None)
    coverage_days = (now - first).days if first else 0
    cutoff = now - timedelta(days=days) if days is not None else None
    in_window = [(t, e) for t, e in timed if cutoff is None or t >= cutoff]
    sessions = {e["session_id"] for _, e in in_window if e.get("session_id")}

    by_name: dict[str, Row] = {}
    lower_map: dict[str, str] = {}
    for copy in copies:
        canonical = lower_map.setdefault(copy.name.lower(), copy.name)
        by_name.setdefault(canonical, Row(canonical)).copies.append(copy)
    pending: dict[tuple, list] = {}
    merged = 0
    by_agent: dict[str, int] = {}
    for stamp, event in sorted(in_window, key=lambda pair: pair[0]):
        name = event.get("skill")
        source = event.get("source")
        if event.get("type") != "load" or not name or source not in SOURCES:
            continue
        canonical_name = lower_map.setdefault(name.lower(), name)
        ag = event_agent(event)
        key = (event.get("session_id"), canonical_name)
        if source == "user-slash":
            pending.setdefault(key, []).append(stamp)
        elif source == "agent-read":
            # A slash command is followed by the agent reading the same SKILL.md; count that once.
            waiting = pending.get(key, [])
            hit = next((s for s in waiting if 0 <= (stamp - s).total_seconds() <= PAIR_SECONDS), None)
            if hit is not None:
                waiting.remove(hit)
                merged += 1
                continue
        row = by_name.setdefault(canonical_name, Row(canonical_name))
        row.counts[source] += 1
        row.agents.add(ag)
        row.by_agent[ag] = row.by_agent.get(ag, 0) + 1
        by_agent[ag] = by_agent.get(ag, 0) + 1
        if event.get("session_id"):
            row.sessions.add(event["session_id"])
        if row.last is None or stamp > row.last:
            row.last = stamp

    latest_evals = store.latest_evals_by_skill(evals or [], agent=agent)
    has_evals = False
    for row in by_name.values():
        ev = latest_evals.get(row.name.lower())
        if ev is not None:
            row.eval_summary = ev
            has_evals = True

    rows = sorted(by_name.values(), key=lambda r: (-r.total, r.name))
    window_days = min(days, coverage_days) if days is not None else coverage_days
    return Summary(
        days if days is not None else coverage_days,
        first,
        coverage_days,
        window_days,
        sessions,
        rows,
        merged,
        agent=agent,
        by_agent=by_agent,
        has_evals=has_evals,
    )


def meets_threshold(summary, min_days, min_sessions):
    return summary.window_days >= min_days and len(summary.sessions) >= min_sessions


def usefulness_label(row: Row, enough: bool) -> str:
    """Combine load evidence with eval lift to classify skill usefulness."""
    ev = row.eval_summary
    if ev is None:
        if row.total:
            return "loaded" if row.copies else "unmatched"
        return "review-candidate" if enough else "not-observed"

    delta = ev.get("benefit_delta")
    rate = ev.get("with_skill_rate")
    strong_lift = (delta is not None and delta >= 0.15) or (delta is None and rate is not None and rate >= 0.7)
    no_lift = (delta is not None and delta <= 0.0) or (delta is None and rate is not None and rate < 0.5)

    if row.total:
        if strong_lift:
            return "high-value"
        if no_lift:
            return "low-benefit"
        return "neutral"
    if strong_lift:
        return "under-triggered"
    if no_lift and enough:
        return "review-candidate"
    return "not-observed"


def _fmt_pct(val) -> str:
    if val is None:
        return "—"
    return f"{round(float(val) * 100)}%"


def _fmt_delta(val) -> str:
    if val is None:
        return "—"
    pct = round(float(val) * 100)
    return f"+{pct}%" if pct >= 0 else f"{pct}%"


def _sources(row):
    return ", ".join(sorted({c.source for c in row.copies}))


def render(summary, min_days, min_sessions):
    enough = meets_threshold(summary, min_days, min_sessions)
    started = summary.first.date().isoformat() if summary.first else "no events recorded yet"
    title_agent = (
        AGENT_LABELS.get(summary.agent, summary.agent)
        if summary.agent and summary.agent not in ("all", "copilot")
        else "Copilot"
    )
    out = [
        f"# {title_agent} skill load report",
        "",
        f"- Window: last {summary.days} days (tracking started: {started})",
        f"- Coverage in window: {summary.window_days} days, {len(summary.sessions)} sessions",
        "- Covers only sessions recorded after the hook was installed; inventory reflects today, not past availability.",
    ]
    if summary.by_agent and (len(summary.by_agent) > 1 or set(summary.by_agent) != {"copilot"}):
        breakdown = ", ".join(
            f"{AGENT_LABELS.get(ag, ag)}: {cnt}" for ag, cnt in sorted(summary.by_agent.items())
        )
        out.append(f"- Loads by agent: {breakdown}")
    if enough:
        out.append(f"- Coverage meets the review threshold ({min_days} days, {min_sessions} sessions).")
    else:
        out.append(
            f"- Insufficient data for review candidates: {summary.window_days} of {min_days} days, "
            f"{len(summary.sessions)} of {min_sessions} sessions."
        )

    out += ["", "## Loads by skill", ""]
    if summary.merged:
        out += [f"{summary.merged} agent reads were merged into the slash load that triggered them.", ""]
    loaded = [r for r in summary.rows if r.total]
    if loaded:
        if summary.has_evals:
            out.append("| Skill | Copies | agent-read | user-slash | skill-tool | Total | Sessions | Last load | Eval | Benefit |")
            out.append("|---|---|---|---|---|---|---|---|---|---|")
            for r in loaded:
                c = r.counts
                ev = r.eval_summary or {}
                ev_str = _fmt_pct(ev.get("with_skill_rate"))
                delta_str = _fmt_delta(ev.get("benefit_delta"))
                out.append(
                    f"| {r.name} | {len(r.copies)} | {c['agent-read']} | {c['user-slash']} | {c['skill-tool']} "
                    f"| {r.total} | {len(r.sessions)} | {r.last.date().isoformat()} | {ev_str} | {delta_str} |"
                )
        else:
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
    if idle:
        for r in idle:
            line = f"- {r.name} ({_sources(r)})"
            if r.eval_summary is not None:
                ev = r.eval_summary
                line += (
                    f" — eval {_fmt_pct(ev.get('with_skill_rate'))}, "
                    f"{_fmt_delta(ev.get('benefit_delta'))} benefit ({usefulness_label(r, enough)})"
                )
            out.append(line)
    else:
        out.append("None.")

    dupes = [r for r in summary.rows if len(r.copies) > 1]
    out += ["", "## Duplicate names", ""]
    for r in dupes:
        out.append(f"- {r.name} ({len(r.copies)} copies: {_sources(r)})")
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
            [
                "skill",
                "copies",
                "sources",
                "paths",
                "agent_read",
                "user_slash",
                "skill_tool",
                "total_loads",
                "sessions",
                "last_load",
                "status",
                "eval_score",
                "baseline_score",
                "benefit_delta",
                "eval_source",
                "usefulness",
                "last_eval",
            ]
        )
        for r in summary.rows:
            if r.total:
                status = "loaded" if r.copies else "unmatched"
            else:
                status = "review-candidate" if enough else "not-observed"
            ev = r.eval_summary or {}
            writer.writerow(
                [
                    r.name,
                    len(r.copies),
                    ";".join(sorted({c.source for c in r.copies})),
                    ";".join(c.path for c in r.copies),
                    r.counts["agent-read"],
                    r.counts["user-slash"],
                    r.counts["skill-tool"],
                    r.total,
                    len(r.sessions),
                    r.last.isoformat() if r.last else "",
                    status,
                    ev.get("with_skill_rate") if ev.get("with_skill_rate") is not None else "",
                    ev.get("baseline_rate") if ev.get("baseline_rate") is not None else "",
                    ev.get("benefit_delta") if ev.get("benefit_delta") is not None else "",
                    ev.get("eval_source", ""),
                    usefulness_label(r, enough),
                    ev.get("ts", ""),
                ]
            )
