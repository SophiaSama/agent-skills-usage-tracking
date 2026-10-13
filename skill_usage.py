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
