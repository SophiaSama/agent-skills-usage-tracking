# Copilot Skill Usage Tracker

For github copilot users to understand their skill usage. Can be used as a reference to cleanup some skills that have been unused for a long period of time.

Local-only tracker of which skills GitHub Copilot loads in VS Code Local sessions. See [docs/design.md](docs/design.md) and [GLOSSARY.md](GLOSSARY.md).

## Use

    py -3.12 skill_usage.py report --days 30
    py -3.12 skill_usage.py report --days 90 --csv report.csv --workspace D:\projects\pkg\mc_sw
    py -3.12 skill_usage.py dashboard
    py -3.12 skill_usage.py dashboard --data-dir data --workspace D:\projects\pkg\mc_sw --output dashboard.html

The dashboard writes a local standalone HTML snapshot (`skill-usage-dashboard.html` in the current directory by default). Open it directly in a browser.
`--workspace` can be repeated to add project skill roots, `--data-dir` selects the event directory, and `--output` selects the HTML path. The dashboard contains skill names and aggregate usage metadata, not skill contents or filesystem paths. Inventory reflects the time the dashboard is generated; “not observed” is not evidence that a skill is unnecessary, and an unmatched load only means there is no current inventory match.

A slash invocation of a skill such as `/grill-me` is followed by the agent reading the skill's `SKILL.md`. The report merges that read into the slash load when it is in the same session within 120 seconds, so one invocation counts once. Raw events keep both records.

## Dashboard
![dashboard example](.\assets\Dashboard_Screenshot.png)

## Data

Events are stored in `data\events-YYYY-MM.jsonl` (metadata only: time, session ID, workspace folder name, skill, load source). Hook failures are logged by error type in `data\hook-errors.log`. Nothing leaves this machine and nothing is deleted automatically.

## Disable

Delete `C:\Users\uie83312\.copilot\hooks\skill-usage.json`.

## Uninstall and erase data

Delete the hook file above, then delete the `C:\Users\uie83312\.copilot\skill-usage` folder.

## Tests

    py -3.12 -m unittest discover -s tests -t . -v
