# Tasks

## 1. Shared aggregation and dashboard data model

- [x] 1.1 Refactor or extend report aggregation to support both the existing rolling 30-day window and an unbounded valid-event history view; add focused tests for source counts, slash/read pairing, sessions, last-load values, and confirm existing report tests remain unchanged.
- [x] 1.2 Build a dashboard data model that aggregates duplicate inventory copies by name, includes zero-load inventory names, separates unmatched event names, and computes both time ranges against one inventory snapshot; test all fields against representative event and inventory fixtures.

## 2. Standalone dashboard rendering

- [x] 2.1 Render the dashboard as one self-contained HTML document with summary cards, labeled stacked source bars, metadata table, range control, and all/observed/not-observed/unmatched filters; add tests for range/filter behavior, empty-event and empty-inventory states, and responsive table handling.
- [x] 2.2 Secure the embedded payload and dynamic rendering by excluding paths and skill contents, safely escaping script-sensitive names, and inserting names as text; add tests proving markup-like names cannot execute and no absolute paths or external assets/requests appear in the document.

## 3. CLI integration and user documentation

- [x] 3.1 Add the `dashboard` CLI subcommand with default and requested output paths plus `--data-dir` and repeatable `--workspace`; add CLI tests for defaults, overrides, generated output, and write failures reporting the path with a nonzero result.
- [x] 3.2 Document dashboard usage, local-only behavior, and snapshot/unmatched-status caveats in the README; verify the documented command and options match CLI help.
- [x] 3.3 Run the complete standard-library unittest suite and verify existing report text/CSV behavior remains unchanged alongside the dashboard tests.
