# Skill Usage Tracking

A personal, local-only tracker of which agent skills GitHub Copilot loads in VS Code, so rarely loaded skills can be reviewed.

## Language

**Skill**:
A named folder with a `SKILL.md` that an agent can load on demand.
_Avoid_: Plugin, prompt, instruction

**Skill copy**:
One physical `SKILL.md` folder at a specific path. The same skill name can have several copies.
_Avoid_: Duplicate, install

**Skill load**:
A recorded event where a skill's instructions entered an agent session (Copilot, Antigravity, Pi Agent, or Hermes Agent), by any load source.
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
