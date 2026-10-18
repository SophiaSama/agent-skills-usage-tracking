# History of Changes (Changelog)

All notable changes, bug fixes, test coverage expansions, and CI/CD pipeline updates to `agent-skills-usage-tracking` are documented in this file.

---

## [Unreleased / 2026-10-18] — Audit Remediation, Multi-Agent Fixes & GitHub Actions CI/CD

Following the comprehensive audit in [`docs/reviews/test_coverage_and_ci_review.md`](docs/reviews/test_coverage_and_ci_review.md), this release resolves 7 codebase and configuration defects, establishes a production-hardened GitHub Actions CI workflow, and expands multi-agent unit test coverage.

### Summary of Fixes

| Change ID | Finding Ref | Severity | Component | Problem | Resolution |
|---|---|---|---|---|---|
| **CHG-01** | `FIND-01` | Critical | `tracker/detect.py` | `_is_paginated_continuation` only checked `"StartLine"` (Antigravity) and `"offset"` (Pi/Claude), ignoring GitHub Copilot's `"startLine"` (camelCase) and `"start_line"` (snake_case). Paginated reads (`startLine > 1`) of large `SKILL.md` files were double-counted as separate skill loads. | Expanded checked keys in `_is_paginated_continuation` to `("StartLine", "startLine", "start_line", "offset", "Offset")`. |
| **CHG-02** | `FIND-02` | Critical | `tracker/sync.py` | `collect_pi_events` checked `isinstance(part.get("arguments"), dict)` and defaulted stringified JSON tool arguments (`"{\"path\": ...}"`) to `{}`, silently dropping `read` tool loads from Pi session transcripts. | Added `json.loads()` deserialization fallback when `arguments` is a `str` before checking for `dict`. |
| **CHG-03** | `FIND-03`, `FIND-19` | Critical | `.github/workflows/ci.yml` | Unconditional ref-level `cancel-in-progress: true` cancels `main` runs on rapid merges, while `cancel-in-progress: false` on a shared `main` group drops intermediate pending commits under GitHub Actions' single-slot queue policy. | Configured SHA-scoped concurrency on `main`: `group: ${{ github.workflow }}-${{ github.ref == 'refs/heads/main' && github.sha || github.ref }}` with `cancel-in-progress: true`. |
| **CHG-04** | `FIND-18` | High | `.gitignore` | Line 6 of `.gitignore` ignored `.github/` wholesale, blocking `.github/workflows/ci.yml` from being tracked by Git. | Narrowed `.gitignore` line 6 from `.github/` to `.github/skills/` so development skills stay ignored while `.github/workflows/` is tracked. |
| **CHG-05** | `FIND-07` | High | `tracker/sync.py` | `install_hooks` registered Antigravity hooks in `~/.gemini/config/hooks.json` without `--agent antigravity`, relying solely on payload heuristics (`conversationId`). | Updated Antigravity hook command registration to append `--agent antigravity`. |
| **CHG-06** | `FIND-06` | High | `tracker/sync.py` | The generated Hermes live plugin (`~/.hermes/plugins/skill-usage/__init__.py`) registered `on_session_start` and `post_tool_call`, omitting user prompt / slash-command hooks. | Added `on_user_prompt` hook registration in the Hermes plugin to capture live leading `/` slash commands as `UserPromptSubmit`. |
| **CHG-07** | `FIND-09` | Medium | `tracker/inventory.py` | Skill discovery checked exact uppercase `"SKILL.md" in filenames`, whereas `detect.py` matched `skill.md` case-insensitively. On case-sensitive Linux filesystems (such as Ubuntu CI runners), lowercase `skill.md` files were missed by inventory scans. | Updated `_flat`, `_walk_skills`, `_plugin_store`, `_pi_package_store`, and `_hermes_skills` to match `skill.md` case-insensitively while preserving on-disk filenames. |
| **CHG-08** | `FIND-10` | Medium | `tracker/report.py` | When `summary.agent == "all"`, `report.render()` fell back to `# Copilot skill load report` instead of `# All agents skill load report`. | Updated header title logic in `report.render()` to output `All agents` when `summary.agent == "all"`. |
| **CHG-09** | `FIND-15` | Low | `tracker/store.py` | `default_data_dir()` did not call `os.path.expanduser()` on `SKILL_USAGE_DATA`, causing paths like `~/data` to resolve to a literal `~` directory. | Applied `os.path.expanduser()` before `.resolve()` on `SKILL_USAGE_DATA` overrides. |

### CI/CD Pipeline Additions
- **Added `.github/workflows/ci.yml`** (`FIND-08`, `FIND-11`–`FIND-13`, `FIND-16`, `FIND-20`):
  - Scoped triggers to `push` (`main` branch and `v*` tags), `pull_request` (`main` branch), and `workflow_dispatch`.
  - Enforced least-privilege root permissions (`contents: read`) and bounded runtime (`timeout-minutes: 5`).
  - Configured Python `3.12` and `3.13` test matrix on `ubuntu-latest` with `fail-fast: false`.
  - Enforced hermetic, bytecode-suppressed execution via `python -I -B -m unittest discover -s tests -t . -v`.
  - Added composite post-test cleanliness check verifying both `git status --porcelain` and absence of ignored default outputs (`skill-usage-dashboard.html`, `data/`).
  - Added `$GITHUB_STEP_SUMMARY` reporting with explicit `shell: bash`.
- **Amended `docs/superpowers/specs/2026-10-14-github-actions-ci-design.md`** (`FIND-17`):
  - Updated specification to reflect SHA-scoped concurrency, `-I -B` execution flags, `.gitignore` unblocking, and composite cleanliness verification.

### Test Suite Coverage Expansions
- **`tests/test_detect.py`**:
  - Added Antigravity `PreInvocation` session start (`invocationNum == 1`) and subsequent invocation rejection (`invocationNum > 1`) tests.
  - Added camelCase (`startLine`), snake_case (`start_line`), PascalCase (`StartLine`), and `offset` pagination continuation tests.
  - Added Claude Code `<command-name>` XML prompt normalization test.
- **`tests/test_multi_agent.py`**:
  - Added Pi Agent transcript sync test with JSON-stringified tool `arguments`.
  - Added Pi Agent `~/.pi/agent/npm/` package store inventory discovery test.
  - Added Antigravity `antigravity-ide` brain directory transcript sync test.
  - Added Claude Code project transcript sync (`collect_claude_events`) and plugin manifest/cache (`_claude_plugin_store`) tests.
  - Added Hermes SQLite `OperationalError("database is locked")` resilience test.
  - Extended `install_hooks` assertions for `--agent antigravity` and Hermes `on_user_prompt`.
- **`tests/test_hook.py`**:
  - Added `hook._parse_agent_arg` tests for `--agent <name>` and `--agent=<name>`.
  - Added `hook.main()` test verifying Antigravity `{"decision":"allow"}` fallback on corrupt JSON stdin.
- **`tests/test_report.py`**:
  - Added multi-agent report rendering test verifying `# All agents skill load report` title and `- Loads by agent:` breakdown line.
- **`tests/test_cli.py`**:
  - Added `--agent` CLI flag filtering test with `--no-sync`.
- **`tests/test_inventory.py`**:
  - Added cross-platform case-insensitive `skill.md` discovery test.
- **`tests/test_store.py`**:
  - Added `SKILL_USAGE_DATA` tilde (`~`) expansion test.

---

## [2026-10-17] — Multi-Agent Skill Usage Collection (`b4b3fd0`)
- Added multi-agent skill inventory scanning, live hook installation (`install-hooks`), and historical session synchronization (`sync`) for **Claude Code**, **Google Antigravity**, **Pi Agent**, and **Hermes Agent**.
- Added per-agent filtering (`--agent`) across CLI subcommands and interactive agent view switchers in the HTML dashboard.

## [2026-10-14] — Interactive Skill Usage Dashboard & CI Spec (`ecbf43d`, `87c8150`)
- Added `skill_usage.py dashboard` command and `tracker/dashboard.py` renderer producing a self-contained, offline HTML/JS dashboard across 30-day and all-history windows.
- Authored initial GitHub Actions CI design specification (`docs/superpowers/specs/2026-10-14-github-actions-ci-design.md`).

## [2026-10-13] — Initial Local Copilot Skill Usage Tracker (`620701c` – `bc3e62f`)
- Implemented `tracker/store.py` (monthly append-only JSONL store with resilient UTF-8 decoding).
- Implemented `tracker/inventory.py` (personal, project, and plugin skill discovery).
- Implemented `tracker/detect.py` (metadata-only extraction from `SessionStart`, `UserPromptSubmit`, and `PostToolUse` hook payloads).
- Implemented `tracker/report.py` (120-second slash-to-read temporal pairing deduplication, 30-day/20-session coverage threshold, Markdown and CSV reporting).
- Implemented `hook.py` (non-blocking hook recorder) and `skill_usage.py` (`report` CLI).
