# pdx-audit

When the game patches, your mod's overrides can silently fall out of sync: a REPLACE keeps overwriting a vanilla block that gained new lines, an INJECT points at a block that moved, a modifier you reference gets renamed. Nothing errors, so the mod just quietly does the wrong thing.

pdx-audit finds these cases by comparing your overrides against a local history of vanilla snapshots, sorts what it finds by severity, and remembers what you have already decided about. The tools to build that history are included, so you set it up once and add a snapshot after each patch.

## The five audits

With no flag, all five audits run. Name one or more to run only those.

- **Override** (`--overrides`): compares every `REPLACE:`/`TRY_REPLACE:` block three ways (vanilla before, your copy, vanilla after) and classifies each vanilla change: a value you kept at vanilla's old value, a line vanilla added that you lack, a line vanilla deleted that you still carry, a value you customized that vanilla also changed, or a deliberate choice (commented out, key removed, already merged). Also reports `INJECT:` targets whose top-level keys vanilla changed, single-value REPLACEs whose vanilla value changed, and overrides whose target vanilla removed.
- **Dependency** (`--deps`): flags names your script uses (keys you write and names you reference) that vanilla used at some tracked version but no longer uses, with the patch that dropped them. It does not guess renames.
- **GUI** (`--gui`): finds GUI templates, types and whole `.gui` files the mod overrides, works out which vanilla version each copy was last synced to, and reports vanilla changes the copy lacks.
- **Localization** (`--loc`): finds localization keys the mod redefines whose vanilla value changed or was removed, matching by `(language, key)` rather than by filename.
- **Duplicate** (`--dupes`): one source of truth per definition. Flags names defined or overridden in more than one place in the mod (two INJECTs, an INJECT and a REPLACE, two definitions, ...), define keys set twice, GUI definitions defined in two mod files, and plain redefinitions of a vanilla name outside vanilla's file.

The mechanics behind each audit are described in [HOW_IT_WORKS.md](HOW_IT_WORKS.md).

## Running it

pdx-audit is pure Python and needs only git. The desktop app that `--display` opens also needs PySide6, which `pipx inject pdx-audit PySide6` (or `pip install ".[app]"`) adds. Run it as `./pdx-audit` from the repo, or install it (for example `pipx install --editable .`) to use the bare `pdx-audit` the examples below assume. The code lives in the `pdxaudit/` package, and the tests run with `python -m pytest`.

## Usage

Run pdx-audit from anywhere inside a mod; it finds the mod root through `.metadata/`, or you can set it with `--mod-root`. The first run needs a vanilla tracker, which is a one-time setup covered below. After a game patch, take a snapshot first, then run the audits.

```bash
pdx-audit                     # run all five audits
pdx-audit --overrides         # just the override check
pdx-audit --deps              # names vanilla no longer uses
pdx-audit --gui               # vanilla GUI changes your copies lack
pdx-audit --loc               # vanilla loc strings that changed under keys you override
pdx-audit --dupes             # duplicate definitions
pdx-audit --deps --gui        # any combination runs just those

pdx-audit --summary           # the ranked summary only, no per-audit detail
pdx-audit --display           # open the desktop app (see Reading the output)
pdx-audit --diff              # show the actual line changes
pdx-audit --block farming_village              # one block by name (skips --deps)
pdx-audit --overrides --category building_types  # one category (with --overrides/--dupes only)
pdx-audit --full              # compare against the oldest snapshot, not just last patch
pdx-audit --old 1.3.8 --new 1.3.10   # pick the two versions (tag or commit hash)
pdx-audit --color never       # plain output; auto colours a terminal, never a pipe
pdx-audit --list-commits      # list snapshots you can pass to --old/--new
pdx-audit --snapshot 1.3.12   # record the current install as a new snapshot, then exit

pdx-audit --dismiss 3f9a1c2b --reason "halved on purpose"   # hide a finding
pdx-audit --undismiss 3f9a1c2b                               # bring it back
pdx-audit --show-dismissed                                   # list dismissed findings
pdx-audit --stamp-fork-points          # save each GUI copy's detected fork point
pdx-audit --remove-orphaned-records    # remove records for commits on no branch
```

`--old` must be older than `--new`. A `--block` or `--category` that matches nothing is an error rather than a clean report.

Bare `pdx-audit` compares the newest two snapshots, which is the one-patch-back check and needs no arguments. It is also the slowest form, because it includes the localization scan, so name a single audit when you only need one.

## Reading the output

Every run opens with a summary that ranks findings across all audits by how much they need attention: **broken** (the override cannot take effect as written, or a definition has more than one source), **stale** (it takes effect but hides content vanilla added), and **review** (it may have drifted and a human has to judge). Findings are grouped by class, so each class states what it is and the one fix for it once, then lists the affected items.

Each item starts with its id, such as `[3f9a1c2b]`. A REPLACE value is shown as a labelled pair, your value above vanilla's change, with the patch in brackets:

```
  - [cc01a5e3] `blast_furnace` `in_game/common/advances/MnT_3_production_method_unlocks.txt:1` requires
      yours:    pop_promotion_speed_age_3
      vanilla:  pop_promotion_speed_age_3  →  plantation_buildings_advance  (1.3.11)
    To keep one as it is: `pdx-audit --dismiss cc01a5e3 --reason "why"`
```

On a terminal the labels and vanilla's old value are dim, vanilla's new value is green, and only the ✗ or ⚠ symbol carries the severity colour. Each group ends with the exact command to dismiss a finding in it, and the summary ends with how long a dismissal lasts and how to undo it. Duplicate definitions carry no id because they cannot be dismissed.

When some findings come from an earlier patch than the one being audited, they are listed under **Still open from earlier patches**. Informational findings and dismissed findings are only counted; dismissed REPLACE lines are also left out of the per-audit detail.

The per-audit detail follows below the summary, and `--summary` prints the summary alone. On a terminal the summary and detail are colourised; piped or redirected output stays plain Markdown.

`--display` opens the desktop app instead. Every run from the app covers all five audits, and the chips in its top bar show how many findings each audit has and hide or show them without a new run. The version window sits beside the chips, and the arrow on **Run audits** holds the category, block and oldest-snapshot choices; a run with a category covers the override and duplicate audits, the two a category applies to. Findings are listed as expandable folders or as a flat list of files, with findings from earlier patches in their own section.

Selecting a finding shows your value against vanilla's change and, for a REPLACE, INJECT or GUI copy, your block with vanilla's changes marked on the lines they affect: orange for a change your block doesn't have (stale), yellow for a value you and vanilla both changed (review). In the gutter, `−` is your line that vanilla changed or deleted and `+` is vanilla's line your copy lacks, shown faintly where it belongs. In a GUI copy, your line is marked as a change when vanilla dropped it, or when it stands where vanilla changed a line: the same key in the same order, the same quoted value under a different key (vanilla moving `onpressed = "[OnPause]"` to `on_action = "[OnPause]"`), or your key standing one for one in place of vanilla's. A one-line block lines up with the same block spread over lines, and when two blocks line up, what changed inside them is highlighted. The words that differ are highlighted in both lines; in a REPLACE, vanilla's line sits right under yours. `~` marks a line to look at for a reason its note gives. Long unchanged stretches fold. The commands above are buttons in the app: **Dismiss** on each finding, **Restore** on the Dismissed page, and **Take snapshot**, **Save fork points** and **Remove orphaned records** on the Tracker page. Audit flags given with `--display` choose which chips start switched on, and `--old`, `--new`, `--full`, `--block` and `--category` fill in the first run.

When you come back to the app after editing the mod, a note beside **Run audits** says how many files changed since the run it is showing; the list stays as it was until you run the audits again. Script in the app is coloured with the EU5 grammar and Paradox Dark theme of the [Paradox Highlight](https://marketplace.visualstudio.com/items?itemName=dragon-archer.paradox-highlight) extension for VS Code by dragon-archer, used under its MIT licence; the copied files and the licence are in `pdxaudit/syntax`.

## Findings records

pdx-audit remembers what you decided and what is still open, without ever writing into the mod. Records live in the per-user data folder:

- Linux: `~/.local/share/pdx-audit` (or `$XDG_DATA_HOME/pdx-audit`)
- Windows: `%LOCALAPPDATA%\pdx-audit`
- macOS: `~/Library/Application Support/pdx-audit`

Inside it, records are keyed by the mod's `id` from `.metadata/metadata.json` and by git commit:

```
<data folder>/<mod id>/commits/<commit hash>.json   # a record as of that commit
<data folder>/<mod id>/record.json                  # a mod that is not in git
<data folder>/<mod id>/results.json                 # the last run the --display app made
```

A record holds your dismissals, the findings still open (with the patch each came from), and the vanilla version each GUI copy was last reviewed against. A run reads the record of the nearest commit in HEAD's first-parent history that has one, and writes a new one under HEAD only when something changed. So each branch sees the decisions made up to where it split off plus its own, and merging a branch keeps the first parent's decisions.

**Dismissing.** A finding's id comes from its content: the target, and for a line finding the key path plus vanilla's old and new values and yours. Line numbers never enter it, so it survives unrelated edits and is the same for anyone on the same game version. A dismissed finding comes back on its own when vanilla's value or yours changes. Duplicate definitions cannot be dismissed.

**Open findings carry forward.** A finding stays reported until you fix or dismiss it, even after later snapshots move the audit window past the patch it came from.

**Orphaned records.** When a record's commit is on no branch of the repository (a rebased or deleted branch), each run prints a one-line note naming the mod id and commits. `pdx-audit --remove-orphaned-records` lists the exact files, asks for confirmation, and removes each one after checking its name and folder. Add `--force` to skip the confirmation in a script; `--force` works only with this command. A separate clone of the same mod shares the data folder, so its unpushed commits can appear here too. pdx-audit never removes folders.

## The vanilla tracker

The audits compare against a history of vanilla files kept in a bare git repo, with one commit per game version. pdx-audit builds and maintains that repo for you:

```bash
pdx-audit --snapshot 1.3.10
```

The first run creates the tracker and commits the current install's `.txt`/`.yml`/`.gui` files, hashed straight from the game folder into git. Run it again after each patch to grow the history. The audits need at least two snapshots, and if the install has not changed, nothing is committed. Every run samples game files against the newest snapshot and warns when the game has patched since.

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

If the history ever gets out of order, snapshot the versions oldest first into a new tracker with `--vanilla-repo <new path>`.

## Config file

To avoid repeating paths on the command line, copy `config.sample.json` to `config.json` and fill in the values you use:

```json
{
  "game_root": "/path/to/Steam/steamapps/common/Europa Universalis V/game",
  "vanilla_repo": "/path/to/vanilla-tracker/repo.git",
  "skip_dirs": ["backup", "wip", "in_game/gui/experimental"],
  "skip_files": ["*.bak", "*_disabled.txt"],
  "merge_types": ["some_type"]
}
```

- **`skip_dirs`**: lists directories to exclude from every scan. An entry matches that directory anywhere (`backup`) or one specific subtree (`in_game/gui/experimental`).
- **`skip_files`**: lists filename globs to exclude from every scan, matched against both the basename and the full path.
- **`merge_types`**: `common/` folders the engine merges across files, in addition to the ones worked out from vanilla (see the duplicate audit in HOW_IT_WORKS.md).

Every setting follows the same precedence: a CLI flag overrides an environment variable, which overrides the config file, which overrides the built-in default. The config file is looked up at `$PDX_AUDIT_CONFIG`, then `~/.config/pdx-audit.json`, then `config.json` next to the tool.

## Baselines

A copy of a vanilla block is usually taken from some game version and then edited. The audits measure drift from the version each copy was last synced to, so they report only what vanilla changed after that point. For each REPLACE block, shadowed GUI definition and replaced `.gui` file the baseline is, in order:

1. a version saved in the findings record: `--stamp-fork-points` saves the detected fork point of every GUI copy (`--refresh` also updates ones already saved);
2. the newest vanilla patch the copy adopted, detected by checking which of vanilla's changes the copy contains;
3. otherwise, the old version of the audit window.

`--full`, `--old` and `--new` turn this off and measure everything from the window's old version. The full logic is described in [HOW_IT_WORKS.md](HOW_IT_WORKS.md#9-baselines-and-adoption).

## Safety

pdx-audit never removes folders, and every file it deletes goes through one helper that checks the file's name and folder first. A test fails the build if any other code deletes files directly or removes anything recursively.
