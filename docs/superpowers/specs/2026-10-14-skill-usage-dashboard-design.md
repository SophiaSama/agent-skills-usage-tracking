# Skill Usage Dashboard — Design

**Status:** Approved in brainstorming; awaiting written-spec review  
**Date:** 2026-10-14  
**Project:** Local Copilot Skill Usage Tracker

## Goal

Add a self-contained, offline HTML dashboard to the existing local tracker. It visualizes recorded loads for every skill in the current inventory, provides source breakdowns, and lets the user switch between the last 30 days and all recorded history.

The dashboard reports observed load metadata only. A skill with zero recorded loads is **not observed**, not proven unused or unnecessary.

## User workflow

Run a new `dashboard` subcommand from the tracker CLI. It reads the existing event files and scans the same skill inventory used by the report command, then writes a standalone HTML file (default: `skill-usage-dashboard.html` in the current directory; overridable with `--output`). Open that file directly in a browser. No server, network connection, browser plugin, or external asset is required.

The command accepts the existing `--data-dir` and repeatable `--workspace` options so the user can select event data and include additional project skill roots. The inventory scope otherwise follows the current scanner: personal skill roots, installed plugin skill roots, and the current workspace.

## Dashboard content and interactions

- **Summary cards:** installed skills, recorded loads, observed skills, and not-observed skills for the selected time range. The installed count is the current inventory count; observed and not-observed counts partition inventoried skill names.
- **Time-range control:** defaults to **Last 30 days** and offers **All recorded history**. Both views use the same current inventory; inventory reflects the time the dashboard is generated, not historical availability.
- **Stacked horizontal bar chart:** one row per distinct skill name, sorted by total loads descending. Segments represent `agent-read`, `user-slash`, and `skill-tool`, with a visible legend. Every inventoried skill appears, including zero-load skills. Duplicate copies are aggregated by name rather than inflating the distinct-skill count.
- **Metadata table and filters:** filters for all skills, observed, not observed, and unmatched loads. For inventoried skills, show copy count and source tags, counts by load source, total loads, distinct sessions, last load, and observed status. Do not display absolute filesystem paths or skill contents.
- **Unmatched-load section:** list each recorded skill name not found in today's inventory, with load count, source categories, and last-seen time. Explain that an unmatched name only means there is no current inventory match; it may have been removed or renamed, or be outside scanned locations. The tracker cannot determine the cause because it does not retain historical inventory snapshots.

The page may require vertical scrolling for large inventories and horizontal scrolling for the metadata table on narrow screens. Labels and status text must make chart segments and zero-load rows understandable without relying on color alone.

## Architecture and data flow

1. The CLI handles `dashboard` arguments, reads events through the existing store, and scans skill copies through the existing inventory module.
2. Existing report aggregation rules are reused for the 30-day view, including slash/read pairing behavior and source categories. The all-history view aggregates all valid dated events in the event store without the 30-day cutoff.
3. A dashboard renderer produces a single HTML document containing the aggregated metadata, styles, and small inline JavaScript needed for the range and list filters. The browser performs no requests and reads no files.
4. The renderer includes inventory rows with zero loads, and keeps unmatched event names separate from installed skills.

The page is a generated snapshot: event and inventory changes are reflected the next time the command is run.

## Privacy and safe rendering

- Reuse only the tracker's existing metadata. Do not include prompts, tool responses, model output, skill file contents, or absolute paths.
- The generated file contains skill names and aggregate usage metadata. It remains at the user-selected local path; there is no upload or telemetry.
- Serialize embedded data safely and escape all event- and inventory-derived text for HTML. Skill names are data, never executable markup.
- No external JavaScript, CSS, fonts, charting library, or other network asset is loaded.

## Empty data and errors

- Invalid JSONL lines and events without valid timestamps continue to be ignored according to existing store/report behavior.
- With no valid events, show all current inventory skills with zero counts and an explanatory empty-data message.
- With no installed skills, show an empty inventory state while still listing any unmatched recorded loads.
- A failure to write the output file is reported by the CLI with the target path and a nonzero exit status. The command must not silently claim that a dashboard was created.
- Unmatched status is descriptive, not an error and not proof that a skill has been deleted.

## Testing and acceptance criteria

Use the existing standard-library `unittest` suite; add focused dashboard tests and CLI coverage. Verify:

1. The generated document contains all inventoried names, including zero-load skills, and aggregates duplicate copies by name.
2. Stacked source counts, totals, sessions, last-load values, observed status, and unmatched-load rows agree with the event data.
3. The 30-day default excludes older loads, while All recorded history includes them; switching ranges updates chart, table, and summary values consistently.
4. Slash/read merging follows the existing report behavior in both ranges.
5. Filters distinguish all, observed, not observed, and unmatched rows, and empty-event/empty-inventory states are clear.
6. Names containing HTML or script-like characters render as text and cannot execute markup.
7. The CLI writes a standalone HTML file to the default or requested output path, supports data/workspace overrides, and reports write failures.
8. The existing report output and tests remain unchanged.

## Out of scope

- A running local web server or hosted dashboard.
- Network access, cross-device sync, scheduled refresh, or background monitoring.
- Capturing more hook data or changing the existing metadata collection/privacy model.
- Historical inventory snapshots, explanations that infer why a load is unmatched, or judgments about skill effectiveness.
- Editing, disabling, removing, or recommending changes to skills.
