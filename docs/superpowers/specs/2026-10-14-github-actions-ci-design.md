# GitHub Actions CI — Design

**Status:** Approved & Implemented  
**Date:** 2026-10-18  
**Project:** Local Copilot Skill Usage Tracker

## Goal

Run the repository's automated tests on GitHub for pushes to `main` (and release tags `v*`), pull requests targeting `main`, and manual dispatches, checking the documented Python 3.12 baseline and Python 3.13.

## Workflow

Ensure `.gitignore` scopes local development skill ignores to `.github/skills/` so `.github/workflows/` is tracked by Git.

Add one workflow at `.github/workflows/ci.yml`. It triggers on:
- `push` to `main` and release tags (`v*`)
- `pull_request` targeting `main`
- manual execution via `workflow_dispatch`

Path filtering is intentionally omitted to avoid GitHub Branch Protection status check deadlocks on documentation changes.

The workflow checks out the repository and executes the standard-library test suite in isolated, bytecode-suppressed mode (`python -I -B -m unittest discover -s tests -t . -v`).

The job runs on `ubuntu-latest` across a matrix of Python `3.12` and `3.13` via `actions/setup-python`, configuring `fail-fast: false` to ensure complete diagnostic visibility across all matrix entries. No package installation or dependency caching is configured because the project strictly relies on the Python standard library.

## Security and execution controls

- Set workflow-level permissions strictly to `contents: read` to enforce least-privilege security.
- Set job execution timeout to `timeout-minutes: 5`.
- Concurrency control uses SHA-scoping on `main` (`group: ${{ github.workflow }}-${{ github.ref == 'refs/heads/main' && github.sha || github.ref }}` with `cancel-in-progress: true`). Pushes to `main` run in parallel to completion without queue drops or serialization bottlenecks; pull request runs cancel superseded builds immediately.
- Do not expose secrets or add deployment behavior.
- Post-test step verifies working-tree cleanliness (`git status --porcelain`) and guards against leaked project-default ignored targets (`skill-usage-dashboard.html`, `data/`).
- Steps specify `shell: bash` and map environment parameters to guarantee cross-platform compatibility.

## Acceptance criteria

1. CI triggers on pushes to `main`, tags matching `v*`, PRs targeting `main`, and manual `workflow_dispatch`.
2. Both Python matrix entries (`3.12` and `3.13`) execute the full unittest discovery command in isolated, bytecode-suppressed mode (`python -I -B`) with `fail-fast: false`.
3. The workflow requires no third-party project dependencies or secrets.
4. Workflow permissions are strictly limited to `contents: read` at the top level.
5. The repository working tree remains clean after test discovery and execution (verified via `git status --porcelain` and checks against project-default ignored targets `skill-usage-dashboard.html` and `data/`).

## Out of scope

Linting, packaging, deployment, scheduled runs, coverage upload, and changes to application code or test behavior.
