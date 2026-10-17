import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from tracker import dashboard, inventory, report, store, sync

AGENT_CHOICES = ("all",) + inventory.ALL_AGENTS
SYNC_AGENT_CHOICES = ("all",) + sync.SYNC_AGENTS


def _maybe_sync(args, home: Path, workspaces: list[Path]) -> None:
    should_sync = getattr(args, "sync", False) or (args.data_dir is None and not getattr(args, "no_sync", False))
    if not should_sync:
        return
    agent_arg = getattr(args, "agent", "all")
    agents = (agent_arg,) if agent_arg in sync.SYNC_AGENTS else None
    sync.sync_events(data_dir=args.data_dir, home=home, workspaces=workspaces, agents=agents)


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
    rep.add_argument("--agent", choices=AGENT_CHOICES, default="all", help="filter report to a specific agent")
    rep.add_argument("--sync", action="store_true", help="sync local agent logs before reporting")
    rep.add_argument("--no-sync", action="store_true", help="skip automatic sync of local agent logs")

    dash = sub.add_parser("dashboard", help="generate a standalone skill usage dashboard")
    dash.add_argument("--workspace", action="append", default=[], type=Path)
    dash.add_argument("--data-dir", type=Path)
    dash.add_argument("--output", type=Path)
    dash.add_argument("--agent", choices=AGENT_CHOICES, default="all", help="filter inventory/sync to a specific agent")
    dash.add_argument("--sync", action="store_true", help="sync local agent logs before building dashboard")
    dash.add_argument("--no-sync", action="store_true", help="skip automatic sync of local agent logs")

    syn = sub.add_parser("sync", help="sync skill usage events from Claude, Antigravity, Pi Agent, and Hermes Agent logs")
    syn.add_argument("--workspace", action="append", default=[], type=Path)
    syn.add_argument("--data-dir", type=Path)
    syn.add_argument("--agent", choices=SYNC_AGENT_CHOICES, default="all", help="sync only a specific agent")

    inst = sub.add_parser("install-hooks", help="install live usage-tracking hooks/extensions for local agents")
    inst.add_argument("--agent", choices=SYNC_AGENT_CHOICES, default="all", help="install hook for a specific agent")

    args = parser.parse_args(argv)
    home = Path.home()

    if args.command == "sync":
        workspaces = args.workspace or [Path.cwd()]
        agents = (args.agent,) if args.agent != "all" else None
        counts = sync.sync_events(data_dir=args.data_dir, home=home, workspaces=workspaces, agents=agents)
        parts = [f"{ag}={counts.get(ag, 0)}" for ag in sync.SYNC_AGENTS if agents is None or ag in agents]
        print(f"Synced {counts['total']} new events ({', '.join(parts)})")
        return 0

    if args.command == "install-hooks":
        agents = (args.agent,) if args.agent != "all" else None
        installed = sync.install_hooks(home=home, agents=agents)
        for ag, path in installed.items():
            print(f"Installed {ag} hook at {path}")
        return 0

    if args.command == "dashboard":
        workspaces = args.workspace or [Path.cwd()]
        output = args.output or Path.cwd() / "skill-usage-dashboard.html"
        _maybe_sync(args, home, workspaces)
        events = store.read_events(args.data_dir)
        copies = inventory.scan(home, workspaces, agent=args.agent if args.agent != "all" else None)
        now = datetime.now(timezone.utc)
        html = dashboard.render_html(dashboard.build_dashboard_data(events, copies, now))
        try:
            output.write_text(html, encoding="utf-8")
        except OSError as exc:
            print(f"error: could not write dashboard to {output}: {exc}", file=sys.stderr)
            return 1
        print(f"Dashboard written to {output}")
        return 0

    workspaces = args.workspace or [Path.cwd()]
    _maybe_sync(args, home, workspaces)
    events = store.read_events(args.data_dir)
    agent_filter = args.agent if args.agent != "all" else None
    copies = inventory.scan(home, workspaces, agent=agent_filter)
    now = datetime.now(timezone.utc)
    summary = report.summarize(events, copies, now, args.days, agent=agent_filter)
    print(report.render(summary, args.min_days, args.min_sessions))
    if args.csv:
        enough = report.meets_threshold(summary, args.min_days, args.min_sessions)
        report.write_csv(summary, args.csv, enough)
    return 0


if __name__ == "__main__":
    sys.exit(main())

