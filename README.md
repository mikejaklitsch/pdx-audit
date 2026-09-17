# pdx-audit

When the game patches, your mod's overrides can silently fall out of sync: a REPLACE keeps overwriting a vanilla block that gained new lines, an INJECT points at a block that moved, a modifier you reference gets renamed. Nothing errors, so the mod just quietly does the wrong thing.

pdx-audit finds these cases by comparing your overrides against a local history of vanilla snapshots, sorts what it finds by severity, and remembers what you have already decided about. The tools to build that history are included, so you set it up once and add a snapshot after each patch. A mod that runs on a framework, or that absorbed code from other mods, can also be compared against those sources.

## The five audits

With no flag, all five audits run. Name one or more to run only those.

- **Override** (`--overrides`): compares every `REPLACE:`, `TRY_REPLACE:` and `REPLACE_OR_CREATE:` block with vanilla's tracked versions of that block and attributes each difference: a change vanilla made that your copy lacks (a value vanilla has since changed, a statement vanilla added, a statement vanilla deleted), a change that meets an edit of yours, or your own edit, which is not reported. Single-value REPLACEs are compared the same way. Also reports `INJECT:`, `TRY_INJECT:` and `INJECT_OR_CREATE:` targets whose top-level keys vanilla changed and overrides whose target vanilla removed.
- **Dependency** (`--deps`): flags names your script uses (keys you write and names you reference) that vanilla used at some tracked version but no longer uses, with the patch that dropped them. It does not guess renames.
- **GUI** (`--gui`): finds GUI templates, types and whole `.gui` files the mod overrides, and compares each copy with vanilla's tracked versions the same way the override audit compares a REPLACE. The changes vanilla made inside one block are one finding.
- **Localization** (`--loc`): finds localization keys the mod redefines whose vanilla value changed or was removed, matching by `(language, key)` rather than by filename.
- **Duplicate** (`--dupes`): one source of truth per definition. Flags names defined or overridden in more than one place in the mod (two INJECTs, an INJECT and a REPLACE, two definitions, ...), define keys set twice, GUI definitions defined in two mod files, localization keys defined more than once, on_action `effect` and `trigger` conflicts, and plain redefinitions of a vanilla name outside vanilla's file.

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
pdx-audit --full              # every audit from the oldest snapshot, not just the last patch
pdx-audit --old 1.3.8 --new 1.3.10   # pick the two versions (tag or commit hash)
pdx-audit --color never       # plain output; auto colours a terminal, never a pipe
pdx-audit --list-commits      # list snapshots you can pass to --old/--new
pdx-audit --snapshot 1.3.12   # record the current install as a new snapshot, then exit

pdx-audit --config            # the settings in use, where each comes from, and the files read
pdx-audit --set vanilla_repo /path/to/my-tracker.git   # point at a tracker under any name
pdx-audit --set game_root "/path/to/Europa Universalis V/game"
pdx-audit --set patch_name Cortes     # the patch name new snapshots record
pdx-audit --unset patch_name          # back to the setting below it

pdx-audit --dismiss 3f9a1c2b --reason "halved on purpose"   # hide a finding
pdx-audit --undismiss 3f9a1c2b                               # bring it back
pdx-audit --show-dismissed                                   # list dismissed findings
pdx-audit --remove-orphaned-records    # remove records for commits on no branch
```

`--old` must be older than `--new`. A `--block` or `--category` that matches nothing is an error rather than a clean report. The options for foundations and adopted sources are listed under [Foundations and adopted sources](#foundations-and-adopted-sources), and in their own group of `pdx-audit --help`.

Bare `pdx-audit` compares REPLACE blocks and GUI copies with every snapshot up to the newest, and runs the dependency and localization audits over the last patch; it needs no arguments. It is also the slowest form, because it includes the localization scan, so name a single audit when you only need one.

## Reading the output

Every run opens with a summary that ranks findings across all audits by how much they need attention: **broken** (the override cannot take effect as written, or a definition has more than one source), **stale** (it takes effect but hides content vanilla added, or a vanilla change meets an edit of yours), and **review** (a vanilla change to check). Findings are grouped by class, so each class states what it is and the one fix for it once, then lists the affected items. Classes fixed file by file, such as duplicate localization keys, list one line per mod file naming every item in it.

Each item starts with its id, such as `[3f9a1c2b]`. A change to a REPLACE or GUI copy is shown as a labelled pair, your statement above what vanilla did, with the patch in brackets:

```
  - [cc01a5e3] `some_building` `in_game/common/building_types/my_buildings.txt:4` modifier
      yours:    cost = 100
      vanilla:  cost = 100  →  cost = 200  (1.3.11)
    To keep one as it is: `pdx-audit --dismiss cc01a5e3 --reason "why"`
```

On a terminal the labels and vanilla's old value are dim, vanilla's new value is green, and only the ✗ or ⚠ symbol carries the severity colour. Each group ends with the exact command to dismiss a finding in it, and the summary ends with how long a dismissal lasts and how to undo it. Duplicates within the mod, such as a name defined in two files, carry no id because they cannot be dismissed; a plain definition of a vanilla name outside vanilla's file has one.

The heading names the versions compared. REPLACE blocks and GUI copies are compared with every snapshot, so when the other audits cover a shorter window, the line below the heading names both.

When some findings come from an earlier patch than the newest, they are listed under **Still open from earlier patches**. A unit compared with more than one base, such as vanilla and an adopted source, is listed once under **Compared with more than one base**, with each base's findings under it. Informational findings and dismissed findings are only counted; dismissed changes are also left out of the per-audit detail.

The per-audit detail follows below the summary, and `--summary` prints the summary alone. For each REPLACE block and GUI copy it lists the changes to take or check, high priority first, each with its line, the patch vanilla made it in, and both sides' text; where vanilla changed a statement you also edited, vanilla's text before its change too. On a terminal the summary and detail are colourised; piped or redirected output stays plain Markdown.

`--display` opens the desktop app instead. Every run from the app covers all five audits, and the chips in its top bar show how many findings each audit has and hide or show them without a new run. The version window sits beside the chips, and the arrow on **Run audits** holds the category, block and oldest-snapshot choices; a run with a category covers the override and duplicate audits, the two a category applies to. Findings are listed as expandable folders or as a flat list of files, with findings from earlier patches in their own section.

Selecting a finding shows your statement against what vanilla did and, for a REPLACE or GUI copy, vanilla's current text beside your copy. Both sides are compared with vanilla's text at the version your copy matches, the way a three-way merge shows two edits of one base: a green `+` line is one that side added since then, and a red `−` line one it deleted, shown with that version's text. Lines both sides kept share a row, so vanilla's edits only appear on the left and yours only on the right; lines are compared without layout, comments or number spelling. A finding's first line carries its severity icon at the left edge, the one the summary prints: `✗` where vanilla changed a block you also edited, `⚠` otherwise. Everything else folds, and each fold says how many of its lines hold changes outside the findings. The header says which patch vanilla changed it in and which version your copy matches. Clear **Side by side** to show your copy alone instead, as an INJECT always is: `−` marks your copy's line and `+` vanilla's current line, shown faintly right under your line or where it belongs, indented the way your copy indents. **Flatten** drops each line's indentation and joins runs of lines that only close blocks into one line, for deeply nested GUI text, and **Wrap lines** wraps long lines to the width of the view instead of scrolling sideways. When both sides are one line, the words that differ are highlighted. Statements line up by key and order, so layout, comments and the spelling of numbers never mark a line, and your own edits carry no mark. Long unchanged stretches fold. The commands above are buttons in the app: **Dismiss** on each finding, **Restore** on the Dismissed page, **Take snapshot** and **Remove orphaned records** on the Tracker page, and the settings on the **Settings** page. Audit flags given with `--display` choose which chips start switched on, and `--old`, `--new`, `--full`, `--block` and `--category` fill in the first run. A duplicate finding shows the source code of each definition open; clear **Expand source code** to show them collapsed.

The **Settings** page holds the same settings `--set` writes, one box each: the tracker, the game folder and the default patch name, with **Browse** for the two paths and **Clear** to fall back to the setting below. Under each box is where the value in use comes from, or a note when a file read earlier is read instead of the one the page writes. It also lists the config files read, marking the one in effect and the one it writes. The patch name is what the Tracker page's snapshot records unless you type another. A change applies at once, so pointing the app at another tracker reloads the snapshots without reopening it, and a `--set` from a terminal is picked up when the page is opened. The boxes are held while a run is reading the tracker they name. With no tracker at all the app still opens, on that page, with the audits switched off until one is chosen or the first snapshot is taken.

When you come back to the app after editing the mod, a note beside **Run audits** says how many files changed since the run it is showing; the list stays as it was until you run the audits again. Script in the app is coloured with the EU5 grammar and Paradox Dark theme of the [Paradox Highlight](https://marketplace.visualstudio.com/items?itemName=dragon-archer.paradox-highlight) extension for VS Code by dragon-archer, used under its MIT licence; the copied files and the licence are in `pdxaudit/syntax`.

## Findings records

pdx-audit remembers what you decided and what is still open, without ever writing into the mod. Records live in the per-user data folder:

- Linux: `~/.local/share/pdx-audit` (or `$XDG_DATA_HOME/pdx-audit`)
- Windows: `%LOCALAPPDATA%\pdx-audit`
- macOS: `~/Library/Application Support/pdx-audit`

Inside it, records are keyed by the mod's `id` from `.metadata/metadata.json` and by git commit, and the settings sit beside them:

```
<data folder>/config.json                           # the settings, for every mod
<data folder>/<mod id>/commits/<commit hash>.json   # a record as of that commit
<data folder>/<mod id>/record.json                  # a mod that is not in git
<data folder>/<mod id>/results.json                 # the last run the --display app made
```

A record holds your dismissals and the findings still open, with the patch each came from. A run reads the record of the nearest commit in HEAD's first-parent history that has one, and writes a new one under HEAD only when something changed. So each branch sees the decisions made up to where it split off plus its own, and merging a branch keeps the first parent's decisions.

**Dismissing.** A finding's id comes from its content: the target, and for a change to a REPLACE or GUI copy the blocks holding it plus your statement and vanilla's, with layout collapsed. Line numbers never enter it, so it survives unrelated edits and is the same for anyone on the same game version. A dismissed finding comes back on its own when vanilla's text or yours changes. Two findings with the same content, such as the same change in two copies of a block, add their file to the id, and within one file their order. Duplicates within the mod cannot be dismissed. `--show-dismissed` and the Dismissed page mark a dismissal whose finding the last run no longer found; `--undismiss` removes it.

**Open findings carry forward.** A finding stays reported until you fix or dismiss it, even after later snapshots move the audit window past the patch it came from. A run of named audits, such as `pdx-audit --gui`, updates only those audits' open findings.

**Orphaned records.** When a record's commit is on no branch of the repository (a rebased or deleted branch), each run prints a one-line note naming the mod id and commits. `pdx-audit --remove-orphaned-records` lists the exact files, asks for confirmation, and removes each one after checking its name and folder. Add `--force` to skip the confirmation in a script. A separate clone of the same mod shares the data folder, so its unpushed commits can appear here too. pdx-audit never removes folders.

## The vanilla tracker

The audits compare against a history of vanilla files kept in a bare git repo, with one commit per game version. pdx-audit builds and maintains that repo for you:

```bash
pdx-audit --snapshot 1.3.10
```

The first run creates the tracker and commits the current install's `.txt`/`.yml`/`.gui` files, hashed straight from the game folder into git. Run it again after each patch to grow the history. The audits need at least two snapshots, and if the install has not changed, nothing is committed. Every run samples game files against the newest snapshot and warns when the game has patched since.

The tracker is a bare git repository under any name, in any folder. It is located on each run by the following precedence:

1. `--vanilla-repo <path>`
2. `$PDX_VANILLA_REPO`
3. the config file's `vanilla_repo`, which `pdx-audit --set vanilla_repo <path>` and the app's Settings page write
4. `<mod-parent>/vanilla-tracker/repo.git`

The install to snapshot is found the same way, in the order `--game-root`, `$PDX_GAME_ROOT`, config `game_root`, then a Steam default; the patch name a snapshot records follows `--patch-name`, `$PDX_PATCH_NAME`, config `patch_name`, then `Pavia`. See [Config file](#config-file).

### Getting a second snapshot

A tracker started today holds only the current patch, and the audits need at least two. You can add older history in any of these ways:

- **Walk Steam back through patches.** In the game's Properties, on the Betas tab, select an older version, let Steam update, then run `pdx-audit --snapshot <version>`. Repeat oldest first up to the current patch. Order matters, because the audits treat git order as patch order, and the tool refuses an out-of-order snapshot so a missed step fails loudly instead of corrupting the history.
- **Snapshot an extracted copy.** Point `--game-root` at any old build you kept or downloaded with DepotDownloader, which fetches a specific historical build you own. Only the `.txt`/`.yml`/`.gui` files matter.

If the history ever gets out of order, snapshot the versions oldest first into a new tracker with `--vanilla-repo <new path>`.

## Foundations and adopted sources

A **foundation** is a mod your mod runs on and loads after, such as a shared framework: its definitions replace vanilla's, and your overrides sit on top of both. An **adopted source** is a mod whose code your mod absorbed, fully or partly, and whose updates you want to keep up with. Both are local folders: a git repository or any folder, such as a Steam workshop item.

**The default workflow is unchanged.** With no sources chosen, a run reports exactly what it reported before. If the mod's metadata declares a dependency that is installed on this machine, a run prints one note on stderr naming it, and the app shows one notice; neither appears in the report. While the stored scan of installed folders is missing or out of date, the note and the notice say to refresh it instead. The note stops when you choose the source or dismiss the suggestion.

**Once you add a foundation**, runs compare the mod against vanilla and its foundations together, so a change a foundation made counts like a change vanilla made, and a definition your mod copies unchanged from a foundation is reported as a duplicate of it. `--vanilla-only` runs against vanilla alone. **Once you add an adopted source**, runs also compare every unit your mod carries from it with that source's history.

### Choosing sources

pdx-audit only suggests sources; you choose them. In the app, the **Sources** page lists your foundations and adopted sources, the versions of the selected one, suggestions with the reason for each, and orphaned sources, and every change there is saved at once. On the command line:

```bash
pdx-audit --sources                                          # choices, versions, patches, freshness, suggestions
pdx-audit --add-source ../some_framework --as foundation     # choose a folder
pdx-audit --add-source ../other_mod --as adopted --kind folder
pdx-audit --add-source ../some_framework.dev --as foundation --replace   # replace a chosen source with the same id
pdx-audit --remove-source some_framework
pdx-audit --relocate-source some_framework /new/path/to/some_framework  # a moved folder keeps its snapshots
pdx-audit --move-source some_framework 1                     # load order among foundations
pdx-audit --set-kind other_mod git                           # git, folder, or auto
pdx-audit --rename other_mod old_ new_                       # read old_ names on its side as new_
pdx-audit --unrename other_mod old_
pdx-audit --ignore-suggestion some_framework
pdx-audit --snapshot-source other_mod                        # record a folder source's current files
pdx-audit --patch some_framework 2.0..2.3 1.3.10             # the vanilla patch those versions belong to
pdx-audit --remove-orphaned-sources                          # stored sources no mod chooses

pdx-audit --vanilla-only                                     # leave the foundations out of this run
pdx-audit --adopted other_mod                                # compare only against this adopted source
pdx-audit --source-version some_framework 2.2                # use an older version of a source
```

Suggestions come from the local mod folder, the workshop folders of every Steam library holding the game, and the folders in the launcher's playsets. A folder is suggested as a foundation when its metadata id matches a dependency your mod declares. While a declared dependency is not chosen, each local git repository with a different id is listed too, so a development clone can stand in for the published copy. Several folders with the same id are all listed. Runs never scan for suggestions: `--sources` and opening the Sources page refresh them, and a run that finds the stored scan out of date says to run `--sources`.

In the app, the arrow on **Run audits** gains a **Compare with** choice once the mod has a source: vanilla and foundations, vanilla only, or one adopted source.

### Stored choices

Your choices are stored per mod, not per commit, so they hold across branches:

```
<data folder>/<mod id>/sources.json
```

```json
{
  "foundations": [
    {"key": "some_framework-3f9a1c", "path": "/path/to/some_framework"}
  ],
  "adopted": [
    {"key": "other_mod-b41e07", "path": "/path/to/other_mod", "kind": "folder",
     "rename": [{"from": "old_", "to": "new_"}]}
  ],
  "ignored_suggestions": []
}
```

Once you dismiss a suggestion, `dependencies_when_ignored` records the dependencies the mod declared at the time, so dismissed suggestions return when those change. A source's id is its metadata id, or its folder name when it has none, and ids are unique among one mod's sources. Relocating a source to a folder whose metadata id differs is refused. Each folder gets a storage key the first time any mod chooses it, and every mod choosing the same folder shares its snapshots and patch assignments. `kind` is detected unless you set it: a folder inside a Steam workshop folder is read as a folder even when it ships a `.git`, since Steam replaces its files without updating that repository; any other folder with a `.git` is read as a git repository. A stored folder that no longer exists is warned about and left out of runs until you relocate or remove it.

### Load order

Foundations load before your mod, in the order you store, and every run assumes that order. New foundations are added last; reorder them with `--move-source` or by dragging on the Sources page. pdx-audit does not read or check your playset's order, so tell players the order your mod needs.

### Versions and snapshots

A git source's versions are the commits along its first-parent history where `.metadata/metadata.json` changes `version`, plus HEAD. A folder source's versions are the snapshots you take with `--snapshot-source` (or **Take snapshot** on the Sources page), tagged with the metadata version, suffixed `.2`, `.3` when the files changed without a version bump. A folder that ships a `.git` has that repository's history imported on its first snapshot. A snapshot whose files match the newest one apart from line endings records nothing. Snapshots of every folder source share one tracker in the data folder, so identical files are stored once.

A run warns when a source changed after its newest recorded version: a workshop item Steam updated, a git source whose HEAD moved or whose working tree has uncommitted changes, or, after a run in the app, a folder whose files no longer match its newest snapshot. Changes to line endings alone are ignored.

### Patches

A foundation version takes part in runs once it belongs to a vanilla patch. A version pdx-audit records gets the newest tracked patch by default, never below the patch of the version before it, and is marked as defaulted; history that existed before you added the source has no patch. Assign patches to one version or a run of consecutive versions with `--patch`, or by selecting versions on the Sources page. Patches never decrease along a source's history, so an assignment that would break this is refused with the version it conflicts with. Adopted sources take no patches, since each is compared with all of its versions. `--sources` lists how many versions of each source have no patch.

### Adopted sources

Every unit your mod defines that exists in an adopted source's history is compared with that source's versions the way a REPLACE is compared with vanilla's: the copy's baseline is the version it differs from least, and each change the source made after it is a finding. Whole files match by path; GUI definitions, script definitions and localization keys match by name, after the source's rename rules. Rename rules rewrite names and file names on the source side that start with `from`, and never touch a name vanilla uses.

Besides the changes, a run reports units the source deleted that your mod still carries (stale) and units the source added to a file your mod carries units from (review; a dismissal lasts until the source changes that unit). Files you carry nothing from are not reported. These findings are worded with the source's name, and its side of each change is labelled `upstream`. An adopted source that itself runs on one of your foundations is compared only by what it adds to that foundation.

### Orphaned sources

A stored source that no mod chooses any more is orphaned. Each run prints a note naming them, and `pdx-audit --remove-orphaned-sources` lists them and asks for confirmation (`--force` skips it). Removal deletes the source's snapshot references, patch assignments and cache files; git drops the unreferenced snapshot data later on its own schedule.

## Config file

Settings that do not change between runs live in a config file: where the tracker is, where the game is installed, and the patch name new snapshots record. Set them on the command line or on the app's Settings page, and they are stored for every mod:

```bash
pdx-audit --config                                     # every setting and where it comes from
pdx-audit --set vanilla_repo /path/to/my-tracker.git   # a tracker under any name, in any folder
pdx-audit --set game_root "/path/to/Europa Universalis V/game"
pdx-audit --set patch_name Cortes
pdx-audit --unset patch_name                           # back to the setting below it
```

`--set` writes the per-user file, `<data folder>/config.json`, beside the findings records. A value it cannot use is refused with the reason and nothing is written: a game folder that is not there, a tracker path that holds something other than a bare git repository, an empty value. A tracker path that does not exist yet, or an empty folder you made in a file dialog, is stored, and `--snapshot` creates the repository there.

The settable keys are `vanilla_repo`, `game_root` and `patch_name`. The rest are edited in the file itself:

- **`skip_dirs`**: lists directories to exclude from every scan. An entry matches that directory anywhere (`backup`) or one specific subtree (`in_game/gui/experimental`).
- **`skip_files`**: lists filename globs to exclude from every scan, matched against both the basename and the full path.
- **`merge_types`**: `common/` folders the engine merges across files, in addition to the ones worked out from vanilla (see the duplicate audit in HOW_IT_WORKS.md).

`config.sample.json` shows every key. Copy it to `config.json` next to the tool to keep settings with the repository instead.

Every setting follows the same precedence: a CLI flag overrides an environment variable (`$PDX_VANILLA_REPO`, `$PDX_GAME_ROOT`, `$PDX_PATCH_NAME`), which overrides the config file, which overrides the built-in default. A `$PDX_VANILLA_REPO` or `vanilla_repo` that points at a missing folder is an error. The config file is the first of these that exists, and it provides every setting:

1. `$PDX_AUDIT_CONFIG`
2. `~/.config/pdx-audit.json`
3. `<data folder>/config.json`, which `--set` and the app write
4. `config.json` next to the tool

If it is not a valid JSON object, a warning says so and its settings are ignored, and `--config` still reports every setting so the file can be found and fixed. Because one file provides everything, creating the per-user file copies in the settings of any file below it that it shadows, so removing a file never loses settings, and `--set` says when a file read before it is read instead of what it just wrote. `--config` marks which file is in effect, and names the path runs fall back to for a setting no file holds.

## Baselines

A copy of vanilla text is usually taken from some game version and then edited. Each REPLACE block, shadowed GUI definition and replaced `.gui` file is compared with vanilla's text of it at every snapshot from `--old` (the oldest snapshot by default) through `--new` (the newest). The copy's baseline is the snapshot it differs from least; among equally close snapshots the oldest wins. Each difference from vanilla's newest text is then looked up in vanilla's history at the same place:

- a value your copy keeps that vanilla has since changed, a statement vanilla added that your copy lacks, or a statement vanilla deleted that your copy still carries, is a change to take;
- a statement you changed or deleted that vanilla also changed after your baseline is a conflict to check;
- a vanilla statement your copy holds as a one-line comment in the same block, word for word (`#trade_income = 0.1`), is your own deletion;
- anything else is your own edit and is not reported.

A change to take is high priority when the block holding it also holds an edit of yours, and mid otherwise. A conflict is mid, since your statement applies before and after vanilla's change, so the game behaves as it did. An INJECT whose top-level key vanilla also changed is stale, since that key's final value is no longer what it was. `--old` narrows the history the comparison can use. The full logic is described in [HOW_IT_WORKS.md](HOW_IT_WORKS.md#4-comparing-a-copy-with-vanillas-history).

## Safety

pdx-audit never writes into your mod or into a source's folder, and never removes folders. Every file it deletes goes through one helper that checks the file's name and folder first. A test fails the build if any other code deletes files directly or removes anything recursively.
