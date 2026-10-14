# Proposal

## Why

The tracker currently reports skill-load aggregates as text/CSV, which makes source mix, observed versus not-observed skills, and unmatched historical names harder to explore. A local, offline dashboard will make this metadata easier to inspect while preserving the tracker's privacy boundary and avoiding claims that unobserved skills are unnecessary.

## What Changes

- Add a CLI command that generates a self-contained HTML dashboard from the current skill inventory and recorded load events.
- Show current inventory and aggregated loads for the last 30 days or all recorded history, including source breakdowns, zero-load skills, duplicate-name aggregation, and unmatched loads.
- Provide in-page range and list filters; disclose that inventory is a current snapshot and unmatched status does not establish why a name has no current match.
- Keep the existing report behavior and output unchanged; make the generated dashboard local-only with no external assets or requests.

## Capabilities

### New Capabilities
- `skill-usage-dashboard`: Generate and inspect a standalone dashboard of current skill inventory and recorded load metadata.

### Modified Capabilities

None.

## Impact

- CLI: `skill_usage.py` gains a dashboard subcommand and output-path handling.
- Tracker: dashboard aggregation/rendering will use the existing event store, inventory scanner, source categories, and slash/read pairing semantics without changing report output.
- Tests: add dashboard rendering and CLI coverage while retaining existing report tests.
- Runtime: standard-library Python and a browser opening the generated local HTML file; no server or external dependency is required.
