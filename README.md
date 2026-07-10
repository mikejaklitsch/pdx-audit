# pdx-audit

Audits a Paradox mod against vanilla patch changes. When the game updates, mod overrides silently drift: a REPLACE block keeps overwriting a vanilla block that gained new lines, an INJECT targets a block that moved or vanished, and modifiers or triggers the mod references get renamed or removed. pdx-audit diffs the mod's override surface against two snapshots of vanilla and reports exactly what changed underneath it.

It runs three audits:

- **Override audit** (default): finds every `INJECT:`/`REPLACE:`/`TRY_INJECT:` directive in the mod, locates the target block in vanilla at the old and new snapshots, and reports what vanilla changed. Changed REPLACE blocks are classified as reconciled or STALE depending on whether the mod's replacement already contains vanilla's new lines. STALE findings are the highest priority output.
- **Dependency audit** (`--deps`): flags `token =` identifiers the mod assigns that vanilla used at the old snapshot but dropped by the new one, meaning the token was likely renamed or removed. Suggests rename candidates.
- **GUI audit** (`--gui`): finds mod GUI template/type definitions that implicitly shadow same-name vanilla definitions, plus same-path `.gui` file replacements, and reports which shadowed vanilla definitions changed.

## Install

Self-contained Python script, no dependencies beyond git. Symlink it onto your PATH:

```bash
ln -s "$(pwd)/pdx-audit" ~/.local/bin/pdx-audit
```

## Setting up the vanilla tracker

The audits need a history of vanilla game files to diff against. That history lives in a bare git repo (the "vanilla tracker") where each commit is one game version. pdx-audit creates and maintains it for you:

```bash
pdx-audit --snapshot 1.3.10
```

The first run creates the tracker repo at `<mod-parent>/vanilla-tracker/repo.git` and commits the current vanilla install's `.txt`/`.yml`/`.gui` files, tagged with the version you pass. Run it again after every game patch to grow the history. Audits need at least two snapshots. If the install did not change, nothing is committed.

The vanilla install is located via `$PDX_GAME_ROOT` (point it at the game's `game/` directory) with a Steam default. `--patch-name` sets the patch name in the commit message. The tracker is discovered per invocation at:

1. `--vanilla-repo <path>` if passed
2. `<mod-parent>/vanilla-tracker/repo.git`
3. `$PDX_VANILLA_REPO`

## Usage

Run from anywhere inside a mod (root found via `.metadata/`):

```bash
pdx-audit                     # override audit, newest two snapshots
pdx-audit --deps              # dependency audit
pdx-audit --gui               # GUI shadowing audit
pdx-audit --full              # widen window to the oldest snapshot
pdx-audit --diff              # include unified diffs for changed blocks
pdx-audit --all               # include unchanged blocks in output
pdx-audit --block farming_village    # audit a single block
pdx-audit --category building_types  # filter to one category directory
pdx-audit --old <hash> --new <hash>  # explicit commit window
pdx-audit --list-commits      # list tracked snapshots
pdx-audit --snapshot 1.3.12   # record a new vanilla snapshot, then exit
```

After a game patch, run the snapshot first, then all three audits. The default window is only the newest two snapshots; use `--full` to catch breakage that landed in an older patch.

## Notes

- Findings are suspects, not confirmed breakage. Verify renames against the game's own documentation before porting.
- On every run a sample of live game files is hashed and compared against the newest snapshot; a warning is printed when the game has patched but the tracker has no snapshot for it.
- Results are cached under `<vanilla-tracker>/cache/`, keyed by commit hash, so cache entries never go stale.
