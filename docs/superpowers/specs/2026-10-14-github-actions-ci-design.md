# GitHub Actions CI — Design

**Status:** Approved in brainstorming; awaiting written-spec review  
**Date:** 2026-10-14  
**Project:** Local Copilot Skill Usage Tracker

## Goal

Run the repository's existing automated tests on GitHub for pushes and pull requests, checking the documented Python 3.12 baseline and the latest stable Python release.

## Workflow

Add one workflow at `.github/workflows/ci.yml`. It triggers on `push` and `pull_request`, checks out the repository, and runs the existing standard-library test suite with `python -m unittest discover -s tests -t . -v`.

The job uses a matrix of Python `3.12` and `3.x` via `actions/setup-python`, where `3.x` resolves to the latest stable release available to the setup action. This retains the documented baseline while checking forward compatibility. No package installation is needed because the project uses only the Python standard library.

## Security and execution controls

- Set workflow permissions to read-only repository contents.
- Use GitHub-hosted Ubuntu runners and a bounded job timeout.
- Cancel an in-progress run when a newer run for the same workflow/ref supersedes it.
- Do not expose secrets or add deployment behavior.

## Acceptance criteria

1. Pushes and pull requests run CI.
2. Both Python matrix entries execute the full unittest discovery command.
3. The workflow requires no third-party project dependencies or secrets.
4. Workflow permissions are limited to `contents: read`.
5. Existing working-tree changes remain untouched.

## Out of scope

Linting, packaging, deployment, scheduled runs, coverage upload, and changes to application code or test behavior.
