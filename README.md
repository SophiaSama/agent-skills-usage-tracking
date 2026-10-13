# Copilot Skill Usage Tracker

Local-only tracker of which skills GitHub Copilot loads in VS Code Local sessions. See [docs/design.md](docs/design.md) and [GLOSSARY.md](GLOSSARY.md).

## Use

    py -3.12 skill_usage.py report --days 30
    py -3.12 skill_usage.py report --days 90 --csv report.csv --workspace D:\projects\pkg\mc_sw

A skill is a review candidate only after at least 30 days and 20 sessions (`--min-days`, `--min-sessions`). Not observed does not mean unnecessary.

A slash invocation such as `/grill-me` is followed by the agent reading the skill's `SKILL.md`. The report merges that read into the slash load when it is in the same session within 120 seconds, so one invocation counts once. Raw events keep both records.

## Data

Events are stored in `data\events-YYYY-MM.jsonl` (metadata only: time, session ID, workspace folder name, skill, load source). Hook failures are logged by error type in `data\hook-errors.log`. Nothing leaves this machine and nothing is deleted automatically.

## Disable

Delete `C:\Users\uie83312\.copilot\hooks\skill-usage.json`.

## Uninstall and erase data

Delete the hook file above, then delete the `C:\Users\uie83312\.copilot\skill-usage` folder.

## Tests

    py -3.12 -m unittest discover -s tests -t . -v
