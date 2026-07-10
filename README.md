# pdx-audit

Audits a Paradox mod against vanilla patch changes. When the game updates, mod overrides silently drift: a REPLACE block keeps overwriting a vanilla block that gained new lines, an INJECT targets a block that moved or vanished, and modifiers or triggers the mod references get renamed or removed. pdx-audit diffs the mod's override surface against two snapshots of vanilla and reports exactly what changed underneath it.

It runs three audits:

- **Override audit** (default): finds every `INJECT:`/`REPLACE:`/`TRY_INJECT:`/`TRY_REPLACE:` directive in the mod, locates the target block in vanilla at the old and new snapshots, and reports what vanilla changed. Changed REPLACE and TRY_REPLACE blocks are classified as reconciled or STALE depending on whether the mod's replacement already contains vanilla's new lines. STALE findings are the highest priority output. A TRY_* directive whose target does not exist in vanilla is reported as expected and non-fatal rather than as a miss.
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
- The tracker contains the game's own script files. Share it privately (it is just a directory), but do not publish it to a public repo; that would be redistributing Paradox's copyrighted files.
