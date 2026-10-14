# Design

## Context

See proposal.md for motivation and specs/skill-usage-dashboard/spec.md for the behavior contract. The current CLI in `skill_usage.py` has a single `report` subcommand, reads JSONL through `tracker.store`, scans skill copies through `tracker.inventory`, and aggregates a rolling window in `tracker.report`. That aggregation includes the `agent-read`, `user-slash`, and `skill-tool` sources and pairs slash/read events within 120 seconds. Inventory copy records contain filesystem paths, so dashboard payload construction must deliberately exclude those paths.

## Goals / Non-Goals

**Goals:**
- Reuse the existing event reading, inventory scanning, source taxonomy, and pairing rules while keeping the report command's output stable.
- Generate one HTML snapshot containing data and presentation logic, with range and row filters available offline.
- Keep the data boundary explicit: skill names and aggregate metadata only, with safe text rendering.

**Non-Goals:**
- Persisting dashboard-specific data, snapshots, or user preferences.
- Adding a web server, browser-side filesystem access, a charting dependency, or another runtime dependency.
- Changing event capture, report semantics, or the skill inventory scanner.

## Decisions

### Keep CLI orchestration thin and isolate dashboard generation

Add a `dashboard` subcommand to `skill_usage.py`, supporting `--output`, `--data-dir`, and repeatable `--workspace`. Follow the existing CLI convention of using the current directory when no workspace is supplied. The command reads events and scans inventory using the same modules as `report`, then calls a focused dashboard renderer/aggregator in a new tracker module. It reports output-write failures with the destination and a nonzero result.

**Alternatives considered:** Put HTML generation into `skill_usage.py` or extend the Markdown/CSV report renderer. A dedicated dashboard module keeps browser presentation separate from existing report output and gives its data model focused unit tests.

### Share aggregation semantics while supporting an unbounded history view

Factor or extend the report aggregation logic so the dashboard can request both the existing 30-day window and all valid dated events without duplicating source counting, inventory-name grouping, session counting, last-load selection, or slash/read pairing. Preserve the `report` command's defaults and output. Build both aggregates before serializing the dashboard so switching the range does not reload files or change the inventory snapshot.

The all-history aggregate has no lower time cutoff; the 30-day aggregate uses the same UTC `now` captured for the generated dashboard. Invalid timestamps and non-load or unsupported-source events remain excluded according to the existing report behavior. Pairing is performed independently within each requested event set, matching the report's current window semantics.

**Alternatives considered:** Call the current fixed-window summarizer with a computed very large day count. This obscures the all-history contract and couples behavior to the oldest event, so a shared aggregation path with an explicit unbounded mode is clearer. Duplicating the report algorithm in the dashboard would risk behavioral drift.

### Embed precomputed, minimal data and use native HTML/CSS/JavaScript

Generate the HTML with summary values for both ranges, one row for each distinct current inventory name (including zero-load names), and a separate list of unmatched event names. Keep the three source counts as chart segments and table fields. A small inline script switches the active aggregate and applies all/observed/not-observed/unmatched filters; it performs no fetches and reads no local files. Use semantic controls and status text, visible source legend, text labels for zero-load rows, and a horizontally scrollable metadata table on narrow viewports.

Serialize the embedded payload as JSON while escaping characters that could terminate or alter an HTML script element (including `<`), and create dynamic visible text with safe text APIs rather than HTML-string insertion. Do not serialize copy paths or skill contents; source tags and counts are sufficient. Render dates in a consistent ISO representation.

**Alternatives considered:** A server-rendered static-only table would not support the requested range/filter controls, while a client-side charting library adds a dependency and network/offline packaging concerns. A custom CSS bar display with plain inline JavaScript is sufficient for the three fixed categories and keeps the file self-contained.

### Treat the dashboard as a generated snapshot

Capture the current inventory and event data at generation time. Both time-range views use that same inventory, and the page explains that today's inventory does not represent historical availability. Not-observed and unmatched labels are descriptive only; do not infer skill effectiveness or why an event name lacks a current inventory match.

**Alternatives considered:** Re-scan skill files from the browser or preserve historical inventory. Browser file access would violate the standalone privacy boundary, and historical inventory requires event/schema changes that are out of scope.

## Risks / Trade-offs

- [A skill name can contain markup-like text] → Escape embedded JSON for script-element safety and render names as text; test malicious-looking names.
- [Current inventory can differ from inventory at event time] → Label the dashboard as a generated snapshot and explain unmatched names without attributing a cause.
- [Large inventories can make the HTML and table long] → Keep the document self-contained and allow vertical page scrolling and horizontal table scrolling; avoid loading every skill's contents.
- [Aggregation refactoring could alter report output] → Preserve existing report invocation and presentation paths and run the full existing unittest suite alongside focused dashboard tests.

## Migration Plan

No event migration or configuration change is required. Add the command and renderer, then validate with the existing unittest suite and focused tests for ranges, pairing, escaping, filters, empty states, and output errors. Rollback consists of reverting the dashboard command/module/tests; recorded event files and existing report output are unaffected.
