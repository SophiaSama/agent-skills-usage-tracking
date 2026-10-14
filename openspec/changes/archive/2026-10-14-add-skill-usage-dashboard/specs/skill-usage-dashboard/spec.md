# Spec Delta

## Purpose

This capability lets users inspect locally recorded skill-load metadata alongside the current skill inventory in a standalone dashboard. It supports exploration without treating lack of observed loads as proof that a skill is unused or unnecessary.

## ADDED Requirements

### Requirement: Generate a standalone dashboard from the CLI
The tracker SHALL provide a dashboard command that writes a self-contained HTML snapshot to the requested output path or to `skill-usage-dashboard.html` in the current directory by default.

#### Scenario: Generate with defaults
- **WHEN** a user runs the dashboard command without an output path
- **THEN** the tracker writes the standalone HTML dashboard to `skill-usage-dashboard.html` in the current directory

#### Scenario: Override data, inventory workspaces, and output
- **WHEN** a user supplies a data directory, one or more workspace paths, and an output path
- **THEN** the dashboard uses those event and inventory inputs and writes the HTML to the requested path

### Requirement: Summarize current inventory and recorded loads
The dashboard SHALL show each distinct inventoried skill name, including names with zero loads, and keep recorded names with no current inventory match separate. Duplicate copies SHALL aggregate by skill name rather than inflate the distinct-skill count.

#### Scenario: Show inventoried skills and usage metadata
- **WHEN** events and inventory contain loads for an inventoried skill
- **THEN** the dashboard shows its copy count, inventory source tags, per-source load counts, total loads, distinct sessions, last-load time, and observed status

#### Scenario: Include zero-load and unmatched names
- **WHEN** an inventoried skill has no loads or an event name has no current inventory match
- **THEN** the dashboard shows the inventoried skill as not observed and lists the unmatched event name separately with its load count, source categories, and last-seen time

#### Scenario: Merge paired slash-command and skill-file-read events
- **WHEN** a slash-command event is followed by a matching skill-file-read event for the same skill and session within the existing pairing interval
- **THEN** the dashboard counts the pair once using the existing report pairing behavior

### Requirement: Switch between time ranges consistently
The dashboard SHALL default to the last 30 days and offer all recorded history; changing the range SHALL update summary cards, chart values, and table values consistently while using the inventory captured when the dashboard was generated.

#### Scenario: Last 30 days is selected initially
- **WHEN** the dashboard is opened
- **THEN** it shows the last-30-days aggregates and identifies that range as selected

#### Scenario: View all recorded history
- **WHEN** a user selects all recorded history
- **THEN** the dashboard includes all valid dated load events available in the event store, including events older than 30 days

### Requirement: Filter and explain dashboard entries
The dashboard SHALL provide filters for all skills, observed skills, not-observed skills, and unmatched loads, and SHALL explain that not-observed and unmatched statuses are descriptive rather than conclusions about skill utility or removal.

#### Scenario: Filter dashboard rows by status
- **WHEN** a user selects an observed, not-observed, or unmatched filter
- **THEN** the displayed rows match the selected category and retain labels that distinguish the category without relying on color alone

#### Scenario: Explain unmatched names
- **WHEN** unmatched loads are displayed
- **THEN** the dashboard explains that no current inventory match was found and that the cause cannot be determined without historical inventory snapshots

### Requirement: Keep the dashboard local and safe to render
The generated dashboard SHALL contain only skill names and aggregate usage metadata, SHALL NOT expose absolute filesystem paths or skill contents, and SHALL render event- and inventory-derived text as data rather than executable markup. It SHALL make no network requests or depend on external assets.

#### Scenario: Render hostile-looking skill names as text
- **WHEN** a skill name contains HTML or script-like characters
- **THEN** the dashboard displays the name as text without executing markup or script

#### Scenario: Open without network access
- **WHEN** a user opens the generated dashboard directly from disk with network access unavailable
- **THEN** its dashboard content and filtering remain available without external requests

### Requirement: Handle empty inputs and output failures clearly
The dashboard SHALL follow existing event-reading behavior for invalid JSONL lines and invalid timestamps, show clear empty states, and report output-write failures with a nonzero command result.

#### Scenario: No valid events
- **WHEN** the event store contains no valid dated events
- **THEN** the dashboard shows current inventory skills with zero counts and an explanatory empty-data message

#### Scenario: No inventory skills
- **WHEN** the current inventory is empty
- **THEN** the dashboard shows an empty-inventory state and still lists any unmatched recorded loads

#### Scenario: Output cannot be written
- **WHEN** the tracker cannot write the dashboard to the requested output path
- **THEN** the CLI reports the target path and exits unsuccessfully without claiming the dashboard was created
