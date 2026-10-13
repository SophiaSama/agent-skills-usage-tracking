# GitHub Copilot Skill Usage Tracker — Design

**Status:** Draft, awaiting user confirmation  
**Date:** 2026-10-13  
**Terms:** see [GLOSSARY.md](../GLOSSARY.md)

## Goal

Provide a private, local-only record of which skills GitHub Copilot loads in VS Code Local-harness sessions, so skills with no observed loads can be reviewed later.

The tracker measures load evidence, not usefulness. A zero count is a review signal, not proof a skill is unnecessary.

## Location

The tracker and its documents are personal and live under `~/.copilot/skill-usage/`. They are not part of the `mc_sw` product repository.

## Scope

- Future VS Code Copilot sessions using the **Local** harness.
- A user-level hook under `~/.copilot/hooks/` so recording works in every workspace.
- Recording skill loads from three load sources: `agent-read`, `user-slash`, `skill-tool`.
- An offline Markdown report with CSV export.
- An inventory of skill copies from Copilot-supported locations, with duplicate names exposed.
- Python standard library only; the hook command uses an explicit interpreter path because the `.venv` is not active when hooks run.

Not included:

- Network access, cloud sync, or cross-device aggregation.
- Capturing prompts, skill contents, tool results, or model reasoning.
- Judging whether a skill improved an outcome.
- Backfilling history from existing debug logs (may be considered later, labeled lower confidence).
- Changing, deleting, or disabling any skill.

## Load sources

| Load source | How it is detected |
|---|---|
| `agent-read` | `PostToolUse` event where a read tool's input path is a `SKILL.md` in a skills folder |
| `user-slash` | `UserPromptSubmit` event whose prompt begins with a slash command matching an inventoried skill name |
| `skill-tool` | `PostToolUse` event for the Skill tool, when enabled, naming a skill |

Each event records its load source; the report shows sources separately. An event that cannot be attributed to a skill reliably is skipped, not guessed.

## Interpretation rules

- **Not observed** is never reported as "unused" or "unnecessary".
- A skill is flagged **review candidate** only when the coverage window is at least 30 days and at least 20 sessions (both configurable). Before that the report says "insufficient data".
- Inventory reflects the report date, not historical availability.
- Effectiveness is not measured.

## Inventory

Scan documented project locations `.github/skills/`, `.claude/skills/`, `.agents/skills/` and personal locations `~/.copilot/skills/`, `~/.claude/skills/`, `~/.agents/skills/`. `.codex/skills/` is not treated as Copilot-discoverable unless VS Code documents it.

Also scan plugin-provided skills under `~/.copilot/installed-plugins/` and `~/.vscode/agent-plugins/`. Each skill copy carries a skill source tag: `project`, `personal`, or `plugin:<name>`.

Each skill copy is its own entry (normalized path and scope). Usage is also aggregated by skill name so duplicate copies do not inflate the distinct-skill count. Duplicate names are flagged in the Markdown report as one line per name with a copy count and sources; the CSV carries every copy path. No cross-location precedence is assumed. Loads that cannot be mapped to an inventory entry are kept as unmatched.

## Architecture and data flow

- A hook configuration under `~/.copilot/hooks/` runs a small recorder on `SessionStart`, `UserPromptSubmit`, and `PostToolUse`. `SessionStart` records a `session` event (timestamp, session ID, workspace folder name) so the report can count sessions that loaded no skill.
- The recorder appends one compact JSON object per skill load to a local JSONL file under `~/.copilot/skill-usage/`. Fields: timestamp, session ID, workspace folder name only (not the full path), skill name, skill copy path (when known), load source. No prompts, tool responses, or skill contents are stored.
- Events go to one file per month (`events-YYYY-MM.jsonl`); nothing is deleted automatically.
- The report runs on demand only, as `python skill_usage.py report --days 30`, printing Markdown and optionally writing a CSV. There is no scheduled job or background process. It reads the event files and scans the inventory, then reports per skill copy and per skill name: loads by source, distinct sessions, last load, and not-observed skills.
- Raw events are never silently deduplicated; the report shows both load counts and distinct-session counts.
- The recorder is non-blocking: it never denies, modifies, or delays tool calls and emits no hook-control output.

## Privacy and failure handling

- Local only. No retention of prompts, responses, reasoning, tool output, or skill contents.
- Validate and normalize paths before deriving a skill name; reject malformed JSON and unrelated input safely.
- If logging fails, do not block the agent; write a short diagnostic that excludes payload content.
- Document how to disable the hook and delete the event file.

## Validation plan

- Unit tests: valid loads for each source, unrelated reads and prompts, malformed payloads, path normalization, repeated loads, slash-prefix edge cases.
- Inventory tests across each documented root, duplicate names, and unmatched loads.
- Capture real Local payloads for `read_file`, the Skill tool, and a slash-command prompt in a disposable session before finalizing detection.
- Confirm stored events contain metadata only.
- Confirm recorder failures never block Copilot work and that the report labels zero-load skills conservatively.

## Open items

- Actual Local tool names and input shapes for reads and the Skill tool (verify from a captured event).
- How a slash-command skill invocation appears in the `UserPromptSubmit` prompt text.
