# Skill Usage Tracking

A personal, local-only tracker of which agent skills coding agents load across sessions, alongside on-demand follow-through and static quality review signals.

## Language

**Skill**:
A named folder with a `SKILL.md` that an agent can load on demand.
_Avoid_: Plugin, prompt, instruction

**Skill copy**:
One physical `SKILL.md` folder at a specific path. The same skill name can have several copies.
_Avoid_: Duplicate, install

**Skill load**:
A recorded event where a skill's instructions entered an agent session (GitHub Copilot, Claude, Antigravity, Pi Agent, Hermes Agent, or Codex), by any load source.
_Avoid_: Use, usage, invocation, consultation, read

**Skill source**:
Where a skill copy comes from: `project`, `personal`, `plugin:<name>`, or `builtin:<agent>`.
_Avoid_: Origin, provider

**Load source**:
How a skill load happened: `agent-read` (agent read its `SKILL.md`), `user-slash` (user typed its slash command), or `skill-tool` (`Skill` or `skill_view` tool call).
_Avoid_: Trigger, origin

**Not observed**:
A current skill copy with no recorded loads in the coverage window. Says nothing about whether the skill is useful.
_Avoid_: Unused, never used, unnecessary, dead

**Review candidate**:
A skill that is not observed after the coverage window meets the minimum size (30 days and 20 sessions by default).
_Avoid_: Removal candidate

**Coverage window**:
The period during which the hook was recording, bounded by the first recorded event.
_Avoid_: History, lifetime

**Support file**:
A non-metadata file inside a skill copy's folder (such as under `scripts/` or `references/`) or an existing local file linked from its `SKILL.md` that an agent may read or execute after loading the skill; excludes `SKILL.md` itself and packaging metadata (`README*`, `LICENSE*`, `CHANGELOG*`, `agents/*`, `package.json`, `pyproject.toml`, `plugin.json`, `manifest.json`).
_Avoid_: Asset, attachment, metadata file

**Follow-through**:
On-demand transcript evidence that, after a skill load in a session, the agent read or ran at least one of that skill's support files (including `skill_view` calls targeting a support `file_path`). Scored only for skill copies with support files; low follow-through (`< 30%` across at least 5 scored sessions by default) is a review signal, not proof of low quality.
_Avoid_: Effectiveness, success rate, compliance

**Quality audit**:
A static, session-independent check of inventoried skill copies covering `SKILL.md` YAML frontmatter, trigger description length and cues, `SKILL.md` line count, local markdown links, and unreferenced support files. Surfaces structural review signals rather than task outcome evaluations.
_Avoid_: Benchmark, task eval, effectiveness score
