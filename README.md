# pdx-audit

Your mod overrides vanilla files. The game gets a patch, vanilla changes, and your copies do not. pdx-audit finds these differences, puts them in order by severity, and keeps a record of the decisions that you made. It never writes in your mod.

## Setup

pdx-audit needs Python and git. Run `./pdx-audit` from the repository, or install it with `pipx install --editable .`.

The audits compare against a history of the vanilla files in a git repository, the tracker. Take the first snapshot now, and one more after each patch:

```bash
pdx-audit --snapshot 1.3.11
```

The audits need two snapshots or more. To add older versions, select an older build on the Betas tab in Steam, let Steam update the files, then snapshot each version, oldest first.

## Use

Run pdx-audit from any folder in the mod. With no option it runs all five audits:

```bash
pdx-audit                     # all five audits
pdx-audit --overrides         # REPLACE and INJECT blocks against vanilla
pdx-audit --deps              # names that vanilla no longer uses
pdx-audit --gui               # vanilla GUI changes that your copies do not have
pdx-audit --loc               # vanilla strings that changed under keys you override
pdx-audit --dupes             # names that the mod defines in more than one place

pdx-audit --summary           # the summary only, with no detail
pdx-audit --diff              # show the line changes
pdx-audit --display           # the desktop app (needs PySide6)
pdx-audit --full              # from the oldest snapshot, not the last patch
```

Each finding starts with an id:

```
  - [cc01a5e3] `some_building` `in_game/common/building_types/my_buildings.txt:4` modifier
      yours:    cost = 100
      vanilla:  cost = 100  →  cost = 200  (1.3.11)
```

Use the id to hide a finding that is correct as it is:

```bash
pdx-audit --dismiss cc01a5e3 --reason "halved on purpose"
pdx-audit --show-dismissed
pdx-audit --undismiss cc01a5e3
```

A dismissal lasts until the text of one side changes.

## Settings

There is one config file, `<data folder>/config.json`, beside the findings records. `pdx-audit --config` gives each setting, its source and the path of the file. `--set` writes it:

```bash
pdx-audit --set vanilla_repo /path/to/my-tracker.git
pdx-audit --set game_root "/path/to/Europa Universalis V/game"
```

A command-line option replaces an environment variable, which replaces the config file, which replaces the default value. `config.sample.json` gives the other keys, such as the folders to leave out of a scan; edit those in the config file itself.

[HOW_IT_WORKS.md](HOW_IT_WORKS.md) gives what each audit compares, and why it reports what it reports.
