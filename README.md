# pdx-audit

Your mod overrides vanilla files. The game gets a patch, vanilla changes, and your copies do not change. pdx-audit finds these differences. It puts them in order by severity, and it keeps a record of your decisions. It never writes in your mod.

## Install

pdx-audit needs Python 3.10 or later, and git.

```bash
pipx install "pdx-audit @ git+https://github.com/mikejaklitsch/pdx-audit.git"
```

## Start

```bash
pdx-audit --display
```

The window opens on the Settings page when there is no tracker yet. You do the rest of the setup there.

## Set the tracker and the game

On the **Settings** page, fill the two boxes and press **Save** under each one.

- **Game folder**: the `game` folder of your EU5 install. **Browse** opens a file dialog.
- **Tracker**: a folder for the vanilla history. Give a path that does not exist yet, and pdx-audit makes the folder. If you have a tracker already, give its path instead. Any name works.

## Build the history

Each commit holds the `.txt`, `.yml` and `.gui` files of one game version, read from your install. The audits compare your mod with these commits. A commit does not read your mod.

**Commit the versions oldest first.** pdx-audit refuses a version that is older than the newest one in the tracker, because the audits read the git order as the patch order. So if you commit the current version first, you cannot add older versions. You must start again with a new tracker.

Decide the oldest version that you want to track. Then do these steps on the **Tracker** page:

1. In Steam, open the Properties of the game. On the Betas tab, select that version.
2. Let Steam update the files.
3. Type the version in **Version**, then press **Commit version**.
4. Do steps 1 to 3 again for each newer version, in order.
5. Select the current version last, and commit it.

The audits need two commits or more. Make one more commit after each game patch. The app shows a note when the game has changed since the newest commit.

## Read the findings

Press **Run audits**. Each chip in the top bar gives the number of findings of one audit. Press a chip to show or hide its findings. Select a finding to see your statement against the change that vanilla made. For a REPLACE or a GUI copy, the app also shows the current text of vanilla next to your copy.

**Dismiss** hides a finding that is correct as it is. A dismissal applies until the text of one side changes. The **Dismissed** page brings one back.

[HOW_IT_WORKS.md](HOW_IT_WORKS.md) gives what each audit compares, and why it reports what it reports.

## The command line

The app runs the audits through this interface, and you can use it to script them. Run pdx-audit in your mod folder, or in a folder below it. It finds the mod root by the `.metadata` folder, and `--mod-root` gives the root directly.

```bash
pdx-audit                     # all five audits
pdx-audit --overrides         # REPLACE and INJECT blocks against vanilla
pdx-audit --deps              # names that vanilla no longer uses
pdx-audit --gui               # vanilla GUI changes that your copies do not have
pdx-audit --loc               # vanilla strings that changed under keys you override
pdx-audit --dupes             # names that the mod defines in more than one place

pdx-audit --summary           # the summary only, with no detail
pdx-audit --diff              # show the line changes
pdx-audit --full              # from the oldest commit, not the last patch
pdx-audit --commit 1.3.12     # record the installed game as a version
```

Each finding starts with an id, which `--dismiss` takes:

```
  - [cc01a5e3] `some_building` `in_game/common/building_types/my_buildings.txt:4` modifier
      yours:    cost = 100
      vanilla:  cost = 100  →  cost = 200  (1.3.11)
```

```bash
pdx-audit --dismiss cc01a5e3 --reason "halved on purpose"
pdx-audit --show-dismissed
pdx-audit --undismiss cc01a5e3
```

`pdx-audit --set` writes the settings that the Settings page writes: `vanilla_repo`, `game_root` and `patch_name`. Edit `skip_dirs`, `skip_files` and `merge_types` in the config file. `pdx-audit --config` shows each setting, where it comes from, and where the file is.

## From a clone

Clone [pdx-utilities](https://github.com/mikejaklitsch/pdx-utilities) next to this repo: pdx-audit's shared helpers live there. Then run `./pdx-audit`, or install with `pipx install --editable .` followed by `pipx inject pdx-audit --editable ../pdx-utilities`, so edits to the shared helpers reach pdx-audit immediately. The tests run with `python -m pytest`.
