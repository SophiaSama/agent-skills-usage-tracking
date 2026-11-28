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
- Judging ground-truth task outcome effectiveness (whether a skill improved a task result without controlled benchmarks).
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
- Ground-truth task outcome effectiveness is not measured without task benchmarks. Instead, two local review signals complement load tracking:
  - **Follow-through** (`tracker/followthrough.py`): scores only loads of skill copies that have non-metadata support or linked files, flagging `< 30%` follow-through across at least `5` scored sessions (both configurable) as a review signal—not proof of low quality, because a task may legitimately require only `SKILL.md`.
  - **Static quality audit** (`tracker/quality.py`): checks frontmatter, trigger descriptions, `SKILL.md` size, local markdown links, and unreferenced support files across all inventoried skills (including zero-session and `SKILL.md`-only skills) as structural hygiene signals rather than task outcome scores.

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

## Verified payload shapes (captured 2026-10-13)

- `read_file` `PostToolUse`: `tool_input.filePath`; events carry `session_id`, `cwd`, ISO `timestamp`.
- Subagent tool calls share the parent `session_id`, and a subagent's prompt arrives as its own `UserPromptSubmit`.
- `SessionStart`: `source` (`new`), `model`, `session_id`, `cwd`.
- Slash invocation: the prompt starts with `/<skill> ...`; the plugin form `/<plugin> <skill> ...` also occurs. After a slash invocation the agent reads the skill's `SKILL.md`, so one invocation produces both a `user-slash` and an `agent-read` record.

## Open items

- The Skill tool was not observed in initial Copilot captures; its tool names and input keys (`SKILL_TOOL_NAMES`, `SKILL_NAME_KEYS` in `tracker/detect.py`) cover observed multi-agent conventions (`Skill`, `skill_view`, etc.).

## Decisions after implementation

- **Slash-read pairing** (`tracker/report.py`): A `user-slash` load absorbs the first `agent-read` of the same skill in the same session within 120 seconds (`PAIR_SECONDS`). Raw events are unchanged; the report states how many reads were merged.
- **Multi-agent inventory, live hooks, and log sync** (`tracker/inventory.py`, `tracker/sync.py`): Extended beyond GitHub Copilot to also scan, hook, and backfill historical session logs for Claude, Antigravity, Pi Agent, Hermes Agent, and Codex, using a 30-second deduplication window when merging synced and hooked events.
- **Antigravity `Path` & `ByteOffset` detection** (`tracker/detect.py`): Added `Path` to `PATH_KEYS` so Antigravity `read_file` calls on `SKILL.md` register as `agent-read`, and added `ByteOffset` (alongside line/page offset keys such as `StartLine`) to `_is_paginated_continuation` so paginated continuation reads of `SKILL.md` are not counted as repeat loads. `skill_view` calls carrying a non-empty `file_path` are likewise ignored by `skill-tool` load detection so reading a skill's internal reference/script file counts as follow-through rather than a new load.
- **On-demand follow-through analysis** (`tracker/followthrough.py`, `skill_usage.py followthrough`): Scans local transcripts across Antigravity, Claude, Pi Agent, Hermes Agent, and Codex on demand to check whether post-load tool calls read or run non-metadata support files in the skill folder or existing local files linked from `SKILL.md` (including `skill_view` reference reads). Stores nothing on disk unless `--csv` is given, and never includes filesystem paths in Markdown output.
- **Static quality audit** (`tracker/quality.py`, `skill_usage.py audit`): Evaluates all inventoried skill copies using Python stdlib only—flagging `missing-frontmatter`, `missing-name`, `missing-description`, `weak-description`, `overlong-description` (`> 1024` chars), `oversized-skill-md` (`> 500` lines), `broken-link` local references in `SKILL.md`, and `unreferenced-file` support files (`info` severity)—without exposing host filesystem paths in Markdown or dashboard JSON.
- **Unified standalone dashboard** (`tracker/dashboard.py`, `skill_usage.py dashboard`): Surfaces per-skill follow-through rates, static quality status/issue codes, and a `Needs attention` filter alongside load counts in a local HTML snapshot.
