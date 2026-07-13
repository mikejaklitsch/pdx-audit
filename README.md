# pdx-audit

When the game patches, your mod's overrides can silently fall out of sync: a REPLACE keeps overwriting a vanilla block that gained new lines, an INJECT points at a block that moved, a modifier you reference gets renamed. Nothing errors, so the mod just quietly does the wrong thing.

pdx-audit finds these cases by diffing your overrides against a local history of vanilla snapshots, sorts what it finds by severity, and can print the exact line changes. The tools to build that history are included, so you set it up once and add a snapshot after each patch.

## The four audits

With no flag, all four audits run. Name one or more to run only those.

- **Override** (`--overrides`): reports what vanilla changed under each `INJECT:`/`REPLACE:`/`TRY_INJECT:`/`TRY_REPLACE:` directive. A REPLACE that no longer contains vanilla's new lines is marked STALE, the highest-priority finding.
- **Dependency** (`--deps`): flags names your script uses that vanilla dropped between the two snapshots, both the keys you write on the left of a statement and the names you reference as a value on the right, like a building or an applied modifier. A name vanilla stopped using was probably renamed or removed, and the audit lists likely renames for each.
- **GUI** (`--gui`): finds GUI templates, types, and whole `.gui` files the mod overrides, then reports which of them vanilla changed.
- **Localization** (`--loc`): finds localization keys the mod redefines whose vanilla value changed or was removed, matching by `(language, key)` rather than by filename.

The mechanics behind each audit are described in [HOW_IT_WORKS.md](HOW_IT_WORKS.md).

## Running it

pdx-audit is pure Python and needs only git. Run it as `./pdx-audit` from the repo, or put it on your PATH to use the bare `pdx-audit` that the examples below assume (for example, symlink it into `~/.local/bin`). The code lives in the `pdxaudit/` package, and the tests run with `python -m pytest`.

## Usage

Run pdx-audit from anywhere inside a mod; it finds the mod root through `.metadata/`, or you can set it with `--mod-root`. The first run needs a vanilla tracker, which is a one-time setup covered below. After a game patch, take a snapshot first, then run the audits.

```bash
pdx-audit                     # run all four audits
pdx-audit --overrides         # just the override check
pdx-audit --deps              # keys vanilla renamed or removed
pdx-audit --gui               # vanilla GUI blocks that changed under your overrides
pdx-audit --loc               # vanilla loc strings that changed under keys you override
pdx-audit --deps --gui        # any combination runs just those

pdx-audit --diff              # show the actual line changes
pdx-audit --block farming_village    # one block by name
pdx-audit --category building_types  # one category directory
pdx-audit --full              # compare against the oldest snapshot, not just last patch
pdx-audit --old 1.3.8 --new 1.3.10   # pick the two versions (tag or commit hash)
pdx-audit --list-commits      # list snapshots you can pass to --old/--new
pdx-audit --snapshot 1.3.12   # record the current install as a new snapshot, then exit
```

Bare `pdx-audit` compares the newest two snapshots, which is the one-patch-back check and needs no arguments. It is also the slowest form, because it includes the localization scan, so name a single audit when you only need one.

## The vanilla tracker

The audits diff against a history of vanilla files kept in a bare git repo, with one commit per game version. pdx-audit builds and maintains that repo for you:

```bash
pdx-audit --snapshot 1.3.10
```

The first run creates the tracker and commits the current install's `.txt`/`.yml`/`.gui` files. Run it again after each patch to grow the history. The audits need at least two snapshots, and if the install has not changed, nothing is committed.

The tracker is located on each run by the following precedence:

1. `--vanilla-repo <path>`
2. `$PDX_VANILLA_REPO`
3. config file (`vanilla_repo`)
4. `<mod-parent>/vanilla-tracker/repo.git`

The install to snapshot is found the same way, in the order `--game-root`, `$PDX_GAME_ROOT`, config `game_root`, then a Steam default. `--patch-name` sets the patch name in the commit message, which defaults to `Pavia`.

### Getting a second snapshot

A tracker started today holds only the current patch, and the audits need at least two. You can add older history in any of these ways:

- **Walk Steam back through patches.** In the game's Properties, on the Betas tab, select an older version, let Steam update, then run `pdx-audit --snapshot <version>`. Repeat oldest first up to the current patch. Order matters, because the audits treat git order as patch order, and the tool refuses an out-of-order snapshot so a missed step fails loudly instead of corrupting the history.
- **Snapshot an extracted copy.** Point `--game-root` at any old build you kept or downloaded with DepotDownloader, which fetches a specific historical build you own. Only the `.txt`/`.yml`/`.gui` files matter.

Snapshots are cheap, derived data, so if the history ever gets tangled you can delete `repo.git` and rebuild it oldest first.

## Config file

To avoid repeating paths on the command line, copy `config.sample.json` to `config.json` and fill in the values you use:

```json
{
  "game_root": "/path/to/Steam/steamapps/common/Europa Universalis V/game",
  "vanilla_repo": "/path/to/.dev-mods/vanilla-tracker/repo.git",
  "skip_dirs": ["backup", "wip", "in_game/gui/experimental"],
  "skip_files": ["*.bak", "*_disabled.txt"]
}
```

- **`skip_dirs`**: lists directories to exclude from every scan. An entry matches that directory anywhere (`backup`) or one specific subtree (`in_game/gui/experimental`).
- **`skip_files`**: lists filename globs to exclude from every scan, matched against both the basename and the full path.

Every setting follows the same precedence: a CLI flag overrides an environment variable, which overrides the config file, which overrides the built-in default. The config file is looked up at `$PDX_AUDIT_CONFIG`, then `~/.config/pdx-audit.json`, then `config.json` next to the tool.

## GUI baseline (fork points)

When you copy a whole `.gui` file to override it, you copy it from some game version and then edit it. The GUI audit reports only what vanilla changed after that point, so it needs to know which version you started from. By default it works this out for each file, comparing your copy against every snapshot and taking the closest as the fork point, which it prints for each file.

To set the fork point yourself, pin the file with a comment on its first line:

```
# pdx-audit fork-point: 1.3.8
```

`--stamp-fork-points` can write these comments for you, but it is niche: the fork point is detected fresh each run, so an unpinned file already tracks vanilla. You pin a file only when you do not intend to keep it in sync with vanilla but still want to see what vanilla changes. `--full` turns detection off and measures every file against the oldest snapshot, which re-reports history your copy already contains, so the per-file default is the one you want for a normal patch check.

The full baseline logic is described in [HOW_IT_WORKS.md](HOW_IT_WORKS.md#9-fork-points).
