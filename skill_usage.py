import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from tracker import dashboard, evals, followthrough, inventory, quality, report, store, sync

AGENT_CHOICES = ("all",) + inventory.ALL_AGENTS
SYNC_AGENT_CHOICES = ("all",) + sync.SYNC_AGENTS
FOLLOW_AGENT_CHOICES = ("all",) + followthrough.FOLLOW_AGENTS


def _maybe_sync(args, home: Path, workspaces: list[Path]) -> None:
    should_sync = getattr(args, "sync", False) or (args.data_dir is None and not getattr(args, "no_sync", False))
    if not should_sync:
        return
    agent_arg = getattr(args, "agent", "all")
    if agent_arg != "all" and agent_arg not in sync.SYNC_AGENTS:
        return
    agents = (agent_arg,) if agent_arg in sync.SYNC_AGENTS else None
    sync.sync_events(data_dir=args.data_dir, home=home, workspaces=workspaces, agents=agents)


def _write_csv(csv_path: Path, writer_fn) -> bool:
    try:
        if hasattr(csv_path, "parent") and csv_path.parent:
            csv_path.parent.mkdir(parents=True, exist_ok=True)
        writer_fn(csv_path)
        return True
    except OSError as exc:
        print(f"error: could not write CSV to {csv_path}: {exc}", file=sys.stderr)
        return False


def _handle_eval(args, home: Path) -> int:
    workspaces = args.workspace or [Path.cwd()]
    agent_filter = args.agent if args.agent != "all" else None
    copies = inventory.scan(home, workspaces, agent=agent_filter)
    evals_dir = args.evals_dir or store.default_evals_dir()

    if args.lint_only:
        seen = set()
        lint_results = []
        for copy in copies:
            key = (copy.name.lower(), copy.path)
            if key in seen:
                continue
            seen.add(key)
            if args.skill and copy.name.lower() != args.skill.lower():
                continue
            lint_results.append(evals.lint_skill(copy))
        if args.json:
            print(
                json.dumps(
                    [
                        {
                            "skill": r.skill,
                            "path": r.path,
                            "ok": r.ok,
                            "issues": r.issues,
                            "has_provider_evals": r.has_provider_evals,
                        }
                        for r in lint_results
                    ],
                    indent=2,
                )
            )
        else:
            for r in lint_results:
                status = "OK" if r.ok else f"ISSUES ({'; '.join(r.issues)})"
                ev_tag = " [has evals]" if r.has_provider_evals else ""
                print(f"- {r.skill}: {status}{ev_tag}")
        return 0 if all(r.ok for r in lint_results) else 1

    target_skills: list[str] = []
    if args.skill:
        target_skills = [args.skill]
    elif args.all:
        seen_names: set[str] = set()
        for copy in copies:
            if copy.name.lower() in seen_names:
                continue
            found, _ = evals.discover_eval_file(copy.name, copies, evals_dir=evals_dir)
            if found is not None:
                seen_names.add(copy.name.lower())
                target_skills.append(copy.name)
    else:
        print("error: specify --skill <name>, --all, or --lint-only", file=sys.stderr)
        return 2

    if not target_skills:
        print("No skills with eval suites found.")
        return 0

    judge = evals.build_judge(args.judge_cmd) if args.judge_cmd else None
    results = []
    for skill_name in target_skills:
        matching = [c for c in copies if c.name.lower() == skill_name.lower()]
        skill_dir = Path(matching[0].path).parent if matching else None
        runner = None
        runner_label = args.runner or args.agent
        if not args.dry_run:
            try:
                runner, runner_label = evals.build_cli_runner(
                    runner_name=args.runner or (None if args.agent == "all" else args.agent),
                    runner_cmd=args.runner_cmd,
                    skill_dir=skill_dir,
                )
            except RuntimeError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
        try:
            res = evals.run_skill_eval(
                skill_name,
                copies,
                runner=runner,
                judge=judge,
                eval_file=args.eval_file,
                evals_dir=evals_dir,
                run_baseline=not args.no_baseline,
                dry_run=args.dry_run,
                agent=args.agent,
                runner_label=runner_label,
            )
        except FileNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if not args.dry_run:
            store.append_eval(evals.eval_record_from_run(res), data_dir=args.data_dir)
        results.append(res)

    if args.json:
        print(json.dumps([evals.eval_record_from_run(r) for r in results], indent=2))
    else:
        for r in results:
            if r.dry_run:
                print(f"[dry-run] {r.skill}: {r.cases_total} cases ({r.eval_source}: {r.eval_path})")
                continue
            score_pct = f"{round((r.with_skill_rate or 0) * 100)}%"
            base_part = (
                f", baseline {round(r.baseline_rate * 100)}%, benefit {round((r.benefit_delta or 0) * 100):+d}%"
                if r.baseline_rate is not None
                else ""
            )
            print(f"{r.skill} ({r.eval_source}): {r.with_skill_passed}/{r.cases_total} passed ({score_pct}{base_part})")
    return 0


def _handle_eval_record(args) -> int:
    if not 0.0 <= args.score <= 1.0:
        print("error: --score must be between 0.0 and 1.0", file=sys.stderr)
        return 2
    if args.baseline is not None and not 0.0 <= args.baseline <= 1.0:
        print("error: --baseline must be between 0.0 and 1.0", file=sys.stderr)
        return 2
    delta = round(args.score - args.baseline, 4) if args.baseline is not None else None
    record = {
        "type": "eval",
        "ts": datetime.now(timezone.utc).isoformat(),
        "skill": args.skill,
        "agent": args.agent,
        "runner": "manual",
        "eval_source": args.source,
        "cases_total": args.cases,
        "with_skill_passed": round(args.score * args.cases),
        "with_skill_rate": round(args.score, 4),
        "baseline_passed": round(args.baseline * args.cases) if args.baseline is not None else None,
        "baseline_rate": round(args.baseline, 4) if args.baseline is not None else None,
        "benefit_delta": delta,
    }
    store.append_eval(record, data_dir=args.data_dir)
    print(f"Recorded eval for {args.skill}: score={args.score:.0%}" + (f", benefit={delta:+.0%}" if delta is not None else ""))
    return 0


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

    syn = sub.add_parser("sync", help="sync skill usage events from Claude, Antigravity, Pi Agent, Hermes Agent, and Codex logs")
    syn.add_argument("--workspace", action="append", default=[], type=Path)
    syn.add_argument("--data-dir", type=Path)
    syn.add_argument("--agent", choices=SYNC_AGENT_CHOICES, default="all", help="sync only a specific agent")

    fol = sub.add_parser("followthrough", help="check whether agents use a skill's own files after loading it")
    fol.add_argument("--days", type=int, help="only sessions started in the last N days (default: all history)")
    fol.add_argument("--min-sessions", type=int, default=followthrough.DEFAULT_MIN_SESSIONS)
    fol.add_argument("--low-rate", type=float, default=followthrough.DEFAULT_LOW_RATE)
    fol.add_argument("--workspace", action="append", default=[], type=Path)
    fol.add_argument("--agent", choices=FOLLOW_AGENT_CHOICES, default="all", help="filter follow-through analysis to a specific agent")
    fol.add_argument("--csv", type=Path, help="write follow-through summary to CSV")

    aud = sub.add_parser("audit", help="audit static quality of installed skills")
    aud.add_argument("--workspace", action="append", default=[], type=Path)
    aud.add_argument("--agent", choices=AGENT_CHOICES, default="all", help="filter audit to a specific agent")
    aud.add_argument("--issues-only", action="store_true", help="show only skills with warnings or info findings")
    aud.add_argument("--csv", type=Path, help="write quality audit summary to CSV")

    inst = sub.add_parser("install-hooks", help="install live usage-tracking hooks/extensions for local agents")
    inst.add_argument("--agent", choices=SYNC_AGENT_CHOICES, default="all", help="install hook for a specific agent")

    ev = sub.add_parser("eval", help="run A/B skill evaluations or static skill lint checks")
    ev.add_argument("--skill", help="skill name to evaluate")
    ev.add_argument("--all", action="store_true", help="evaluate all inventoried skills that have eval suites")
    ev.add_argument("--agent", choices=AGENT_CHOICES, default="all", help="target agent context")
    ev.add_argument("--runner", choices=tuple(evals.DEFAULT_CLI_RUNNERS), help="CLI runner adapter to execute prompts")
    ev.add_argument("--runner-cmd", help="custom command template containing {prompt}")
    ev.add_argument("--judge-cmd", help="optional LLM-as-judge command template containing {prompt}")
    ev.add_argument("--eval-file", type=Path, help="explicit evals.json path")
    ev.add_argument("--evals-dir", type=Path, help="directory of user-authored <skill>.json eval suites")
    ev.add_argument("--workspace", action="append", default=[], type=Path)
    ev.add_argument("--data-dir", type=Path)
    ev.add_argument("--no-baseline", action="store_true", help="skip without-skill baseline arm")
    ev.add_argument("--dry-run", action="store_true", help="validate eval discovery without running any agent CLI")
    ev.add_argument("--lint-only", action="store_true", help="run static SKILL.md health checks only")
    ev.add_argument("--json", action="store_true", help="print JSON output")

    ev_rec = sub.add_parser("eval-record", help="record an eval result into the local evals store")
    ev_rec.add_argument("--skill", required=True)
    ev_rec.add_argument("--score", required=True, type=float, help="with-skill pass rate (0.0 to 1.0)")
    ev_rec.add_argument("--baseline", type=float, help="without-skill baseline pass rate (0.0 to 1.0)")
    ev_rec.add_argument("--source", choices=("provider", "user", "external"), default="user")
    ev_rec.add_argument("--cases", type=int, default=1)
    ev_rec.add_argument("--agent", choices=AGENT_CHOICES, default="all")
    ev_rec.add_argument("--data-dir", type=Path)

    args = parser.parse_args(argv)
    home = Path.home()

    if args.command == "eval":
        return _handle_eval(args, home)

    if args.command == "eval-record":
        return _handle_eval_record(args)

    if args.command == "sync":
        workspaces = args.workspace or [Path.cwd()]
        agents = (args.agent,) if args.agent != "all" else None
        counts = sync.sync_events(data_dir=args.data_dir, home=home, workspaces=workspaces, agents=agents)
        parts = [f"{ag}={counts.get(ag, 0)}" for ag in sync.SYNC_AGENTS if agents is None or ag in agents]
        print(f"Synced {counts['total']} new events ({', '.join(parts)})")
        return 0

    if args.command == "followthrough":
        workspaces = args.workspace or [Path.cwd()]
        agent = args.agent if args.agent != "all" else None
        copies = inventory.scan(home, workspaces, agent=agent)
        sessions = list(followthrough.collect_sessions(home, agent))
        rows = followthrough.analyze(sessions, copies, datetime.now(timezone.utc), args.days, agent)
        print(followthrough.render(rows, args.min_sessions, args.low_rate, args.days))
        if args.csv and not _write_csv(
            args.csv, lambda p: followthrough.write_csv(rows, p, args.min_sessions, args.low_rate)
        ):
            return 1
        return 0

    if args.command == "audit":
        workspaces = args.workspace or [Path.cwd()]
        agent = args.agent if args.agent != "all" else None
        copies = inventory.scan(home, workspaces, agent=agent)
        summary = quality.audit(copies)
        print(quality.render(summary, issues_only=args.issues_only))
        if args.csv and not _write_csv(args.csv, lambda p: quality.write_csv(summary, p)):
            return 1
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
        eval_records = store.read_evals(args.data_dir)
        agent_filter = args.agent if args.agent != "all" else None
        copies = inventory.scan(home, workspaces, agent=agent_filter)
        sessions = list(followthrough.collect_sessions(home, agent_filter))
        now = datetime.now(timezone.utc)
        html = dashboard.render_html(
            dashboard.build_dashboard_data(events, copies, now, sessions=sessions, evals=eval_records)
        )
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
    eval_records = store.read_evals(args.data_dir)
    agent_filter = args.agent if args.agent != "all" else None
    copies = inventory.scan(home, workspaces, agent=agent_filter)
    now = datetime.now(timezone.utc)
    summary = report.summarize(events, copies, now, args.days, agent=agent_filter, evals=eval_records)
    print(report.render(summary, args.min_days, args.min_sessions))
    if args.csv:
        enough = report.meets_threshold(summary, args.min_days, args.min_sessions)
        if not _write_csv(args.csv, lambda p: report.write_csv(summary, p, enough)):
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
