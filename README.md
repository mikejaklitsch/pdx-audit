# pdx-audit

pdx-audit tracks the changes vanilla makes to script, GUI, and localization blocks that your mod also overrides. When the game patches, your overrides can fall out of sync without any error: a REPLACE block keeps overwriting a vanilla block that gained new lines, an INJECT points at a block that moved, or a modifier the mod references gets renamed. pdx-audit finds those cases, sorts them by severity, and can print targeted diffs so you see exactly what changed.

It works by diffing your overrides against a local repository of vanilla snapshots. The tools to build and maintain that repository are included, so you only set it up once and add a snapshot each patch.

It runs four audits (or all of them at once with `--all`):

- **Override audit** (default): finds every `INJECT:`/`REPLACE:`/`TRY_INJECT:`/`TRY_REPLACE:` directive in the mod, finds the matching vanilla block in the old and new snapshot, and reports what vanilla changed. REPLACE and TRY_REPLACE blocks are marked reconciled or STALE depending on whether your replacement already contains vanilla's new lines; STALE is the highest-priority result. A `TRY_*` directive whose target is absent from both snapshots is listed as expected rather than an error, since `TRY_` is meant for targets that may not be present (from another mod, or a conditional vanilla block). A target vanilla actually removed between the two snapshots is still flagged as an orphaned override, `TRY_` or not.
- **Dependency audit** (`--deps`): flags tokens the mod assigns (`token = ...`) that vanilla used in the old snapshot but dropped in the new one, so they were probably renamed or removed. Suggests likely renames.
- **GUI audit** (`--gui`): finds mod GUI templates or types that override a vanilla definition of the same name, plus same-path `.gui` file replacements, and reports which of those vanilla definitions changed.
- **Localization audit** (`--loc`): finds loc keys the mod redefines whose vanilla value changed or was removed between snapshots. Matching is by `(language, key)`, not by filename, because loc keys are not unique across languages and vanilla moves them between files.

## Install

Pure Python, no dependencies beyond git. Symlink the `pdx-audit` entry script onto your PATH; it resolves its own location and finds the `pdxaudit/` package next to it, so the symlink is all you need:

```bash
ln -s "$(pwd)/pdx-audit" ~/.local/bin/pdx-audit
```

Layout: `pdx-audit` is a thin entry point; the code lives in the `pdxaudit/` package (`tracker`, `overrides`, `gui`, `loc`, `report`, `cli`), with tests under `tests/` (run `python -m pytest`).

## Setting up the vanilla tracker

The audits need a history of vanilla game files to diff against. That history lives in a bare git repo (the "vanilla tracker") where each commit is one game version. pdx-audit creates and maintains it for you:

```bash
pdx-audit --snapshot 1.3.10
```

The first run creates the tracker repo at `<mod-parent>/vanilla-tracker/repo.git` and commits the current vanilla install's `.txt`/`.yml`/`.gui` files, tagged with the version you pass. Run it again after every game patch to grow the history. Audits need at least two snapshots. If the install did not change, nothing is committed.

The vanilla install is located via `--game-root` or `$PDX_GAME_ROOT` (point either at the game's `game/` directory) with a Steam default. `--patch-name` sets the patch name in the commit message. The tracker is discovered per invocation at:

1. `--vanilla-repo <path>` if passed
2. `<mod-parent>/vanilla-tracker/repo.git`
3. `$PDX_VANILLA_REPO`

## Back-populating history

A tracker started today has only the current patch, and audits need at least two snapshots. If prior patches are relevant to your workflow, walk your Steam install through them oldest first and snapshot each one:

1. In Steam, open the game's Properties, Betas tab, and select the oldest version you care about. Paradox keeps previous patches selectable there.
2. Let Steam update, then run `pdx-audit --snapshot <version>`.
3. Select the next version, update, snapshot again. Repeat until you are back on the current patch.

Each snapshot reads the live install, so nothing needs to be copied. Order matters because the audits treat git order as patch order; the tool refuses an out-of-order version, so a missed step fails loudly instead of corrupting the history. If you do end up needing an older version after tracking a newer one, delete `repo.git` and rebuild in order. Snapshots are cheap, derived data.

When rolling the install back and forth is not practical:

- `--game-root /path/to/copy/game` snapshots any extracted copy of a version, for example a backup you kept, or an old build downloaded with DepotDownloader (an open-source tool that logs into Steam with your own account and downloads a specific historical build of a game you own, without touching your live install). Only the `.txt`/`.yml`/`.gui` files matter.
- Copying someone's existing `vanilla-tracker/` directory next to your mods gives you their full history with no snapshotting at all; it is self-contained.

Back-populating is optional. Two snapshots (the patch you last verified your mod against and the current one) cover the default audit; deeper history only widens what `--full` can see.

## Usage

Run from anywhere inside a mod (root found via `.metadata/`):

```bash
pdx-audit                     # run all four audits (override, deps, GUI, loc)
pdx-audit --overrides         # just the override check: what changed under your INJECT/REPLACE blocks
pdx-audit --deps              # find modifiers/triggers the mod uses that vanilla renamed or removed
pdx-audit --gui               # find vanilla GUI blocks that changed under your GUI overrides
pdx-audit --loc               # find vanilla loc strings that changed under keys the mod overrides
pdx-audit --deps --gui        # name any combination to run just those
pdx-audit --gui --stamp-fork-points  # write a fork-point comment atop each replaced GUI file (asks first)
pdx-audit --full              # check against the oldest snapshot instead of just the last patch
pdx-audit --diff              # show the actual line changes for each flagged block
pdx-audit --include-unchanged # also list blocks/keys vanilla left unchanged
pdx-audit --block farming_village    # audit just one block by name
pdx-audit --category building_types  # audit just one category directory
pdx-audit --old 1.3.8 --new 1.3.10   # pick the two versions to compare (tag or commit hash)
pdx-audit --list-commits      # list the snapshots you can pass to --old/--new
pdx-audit --snapshot 1.3.12   # record the current install as a new snapshot, then exit
```

After a game patch, run the snapshot first, then the audits. Bare `pdx-audit` runs all four against the newest two snapshots: that is the one-patch-back check, and it needs no arguments or hashes. (It is the slowest way to invoke the tool because it includes the localization scan; name `--overrides`, `--deps`, `--gui`, or `--loc` to run just what you need.) Use `--full` to reach the oldest snapshot, or `--old`/`--new` for any other window; both accept version tags (e.g. `--old 1.3.8 --new 1.3.10`) or commit hashes, listed by `--list-commits`.

### How the GUI audit picks its baseline

When you copy a whole GUI file to override it, you copy it from *some* game version, then edit it. The audit needs to know which version, so it can ask the right question: "has the game changed this file since the version I started from?" Anything the game changed *before* that point is already baked into your copy and is not worth reporting.

By default the GUI audit works this out per file: it compares your copy against every tracked snapshot and takes the closest one as your starting point (your "fork point"), then reports only what the game changed after it. The report lists the fork point it picked for each file, so the guess is never hidden. If it guesses wrong, pin the file: put a comment at the very top,

```
# pdx-audit fork-point: 1.3.8
```

and the audit uses that version instead. `--stamp-fork-points` writes those comments for you, showing the full list and asking before it touches any file.

If a pinned file later drifts (you updated it to a newer patch but left the comment behind), the audit prints a one-line warning that the pin looks stale. `--stamp-fork-points --refresh` rewrites those stale pins, again showing each change and asking first.

`--full` turns this off and compares every file against the *oldest* snapshot instead. That re-reports the game's entire history, including all the old changes your copy already contains, so it flags far more than you need to act on. It is occasionally useful for a from-scratch review, but the default per-file baseline is the one you want for a normal patch check.
