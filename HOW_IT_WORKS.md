# How pdx-audit Works

A technical walkthrough of what the tool does behind the scenes. The README covers usage; this document covers mechanism. It assumes you have read the README and want to understand *why* the audit reports what it reports.

## Contents

1. [The problem being solved](#1-the-problem-being-solved)
2. [The vanilla tracker](#2-the-vanilla-tracker)
3. [The override surface](#3-the-override-surface)
4. [Comparing a copy with vanilla's history](#4-comparing-a-copy-with-vanillas-history)
5. [Override audit](#5-override-audit)
6. [Dependency audit](#6-dependency-audit)
7. [Localization audit](#7-localization-audit)
8. [GUI audit](#8-gui-audit)
9. [Baselines and attribution](#9-baselines-and-attribution)
10. [Findings records](#10-findings-records)
11. [Duplicate audit](#11-duplicate-audit)
12. [Sources](#12-sources)
13. [Flattening](#13-flattening)
14. [The mod against its foundations](#14-the-mod-against-its-foundations)
15. [Adopted sources](#15-adopted-sources)
16. [Caching](#16-caching)
17. [Deletion safety](#17-deletion-safety)
18. [Version window reference](#18-version-window-reference)

---

## 1. The problem being solved

A mod overrides pieces of the base game. When the game patches, the base game moves and the mod's copies do not. Several things can go wrong:

- A block you replaced wholesale gains new lines or new values in vanilla that your replacement now silently drops.
- A block you injected into moves or disappears, so your injection lands nowhere.
- A modifier or trigger you reference gets renamed or removed, so your reference is dead.
- The same definition ends up overridden from two places in the mod, so which one wins depends on file order.

None of these produce an error at load time. The mod just quietly does the wrong thing. pdx-audit exists to make that drift visible before it ships, without re-reporting decisions you already made. The same drift happens against a framework your mod runs on and against another mod whose code you copied, so pdx-audit can compare against those too (sections 12 to 15).

---

## 2. The vanilla tracker

The audit needs the game's files at more than one point in time. It keeps them in a bare git repository called the **vanilla tracker**, where each commit is one game version:

```
23272f5  1.3.11 Pavia      <- newest
cef54d2  1.3.10 Pavia
771f2ce  1.3.8 Pavia
...
741b7ea  1.2.0 Echinades   <- oldest
```

Each commit holds the `.txt`, `.yml`, and `.gui` files from that version's install. `--snapshot <version>` reads the live install and adds a commit: every matching file is hashed straight from the game folder into git (`git hash-object --stdin-paths --no-filters`, so bytes are stored exactly), an index is built from those hashes, and a commit is written. Nothing is copied to a temporary folder; the only temporary item is one index file inside the tracker repo, removed afterwards. If the resulting tree equals the previous snapshot's, nothing is committed.

On every run a sample of live game files, from localization, `gui`, and the `common` script folders, is hashed and compared with the newest snapshot. A mismatch means the game patched but the tracker was not updated, and a warning tells you to run `--snapshot`.

Two terms used throughout:

- **old / new**: the audit window. `new` is the newest snapshot unless `--new` moves it. The override and GUI audits compare REPLACE blocks and GUI copies with every snapshot from `old` through `new`, where `old` is the oldest snapshot unless `--old` moves it. The dependency, localization and INJECT checks use the snapshot before `new` as `old`, unless `--old` or `--full` moves it. `--old` must be older than `--new`.
- **version tag**: the first word of a snapshot's commit message (`1.3.10`). Records store tags, never tracker commit hashes, because every user builds their own tracker.

---

## 3. The override surface

The tool scans the mod for the places it overrides vanilla. In script files that means directives like:

```
REPLACE:some_block = { ... }             # replace vanilla's block entirely
REPLACE:some_value = 5                   # replace a single-value definition
INJECT:some_block = { ... }              # add to vanilla's block
TRY_REPLACE:maybe_block = { ... }        # replace only if the target exists
TRY_INJECT:maybe_block = { ... }         # inject only if the target exists
REPLACE_OR_CREATE:some_block = { ... }   # replace, or create the block if it does not exist
INJECT_OR_CREATE:some_block = { ... }    # inject, or create the block if it does not exist
```

`REPLACE:` and `INJECT:` are errors when the target does not exist; the `TRY_` and `_OR_CREATE` forms are not.

In GUI files the override is implicit: if the mod defines a `template` or `type` with the same name as a vanilla one, or ships a `.gui` file at the same path as a vanilla file, it overrides it. There is no keyword; sameness of name or path *is* the override. This is why the GUI audit is a separate pass with its own logic (section 8).

### How the engine resolves overrides

pdx-audit follows these rules wherever it has to decide what the game ends up with:

| Rule |
|---|
| Vanilla loads first, then mods in load order. |
| Between mods, the later-loaded mod wins on an exact file path or an exact name: a GUI template or type, a plain definition, a localization key. |
| Within one mod, among files defining the same thing, the alphanumerically first file name wins, case-insensitive and numbers included (`00_` and `aaa_` beat `zzz_`). |
| REPLACE becomes the new definition; INJECT adds its insides to the current definition, whatever load order has produced so far. |
| Merging types (on_actions and similar) combine in load order, and within one mod in that same file order. |
| A localization key in `localization/<language>/replace/` overrides the same key in the mod's other localization files. |
| Inside one on_action block, a second `effect` or `trigger` is a syntax error; the same on_action with `effect` or `trigger` in two blocks of one mod is a definition conflict. |
| In an on_action, a later layer's `effect` or `trigger` replaces the earlier one, vanilla's included; other keys combine. |

| Directive | Object missing | Effect |
|---|---|---|
| `INJECT:key` | error | adds the insides into the current object |
| `REPLACE:key` | error | replaces the current object with this definition |
| `TRY_INJECT:key` | ignored | adds the insides into the current object |
| `TRY_REPLACE:key` | ignored | replaces the current object with this definition |
| `INJECT_OR_CREATE:key` | created | adds the insides into the current object, or creates it from them |
| `REPLACE_OR_CREATE:key` | created | replaces the current object, or creates it from this definition |

A mod loads after vanilla, so a mod's definition overrides vanilla's of the same name whatever its file is called; file order only decides between the files of one mod.

---

## 4. Comparing a copy with vanilla's history

A REPLACE block, a shadowed GUI definition and a replaced GUI file are all a **copy**: vanilla text taken at some game version and then edited. For each difference between your copy and vanilla's newest text, pdx-audit works out **who made it**.

### Lining up statements

The copy and vanilla's newest text are parsed into statements: `key = value`, a block, or a run of bare values such as `0.0 0.0 0.0 1.0`. Sibling statements pair in order, trying in turn:

1. the same statement;
2. the same key (a block that sets `name = "..."` is known by that name first, then by its key alone);
3. the same distinctive quoted value under another key, such as vanilla moving `onpressed = "[OnPause]"` to `on_action = "[OnPause]"`.

What is still unpaired pairs by key out of order, so a moved block still pairs. Paired blocks are compared the same way inside. Layout, comments and the spelling of numbers (`0.10` versus `0.1`) never count as a difference. A statement vanilla has that your copy holds only as a one-line comment in the same block, word for word apart from layout (`#trade_income = 0.1`), is your own deletion: you saw vanilla's statement and turned it off. A comment that reads as anything more than one statement, such as prose, is ignored. Repeated statements in one block, such as list members, compare as a multiset, so their order never matters. A REPLACE block is compared by its contents, since its own key (`REPLACE:some_block`) differs from vanilla's.

### Attributing each difference

Each difference is looked up in vanilla's tracked history at the same place: the keys of the enclosing blocks plus the statement's own key. The copy's **baseline** (section 9) decides whether vanilla made a change before or after the copy was taken.

| Change | Meaning | Priority |
|--------|---------|----------|
| vanilla changed | your value is one vanilla had at this place; vanilla has changed it | high or mid |
| vanilla added | vanilla added the statement after the block existed; your copy lacks it | high or mid |
| vanilla removed | your copy carries a statement vanilla had here and has deleted | high or mid |
| both changed | vanilla changed a statement you changed too, or deleted one you changed, after your baseline | mid |
| removed changed | vanilla changed a statement you deleted, after your baseline | mid |
| your edit | vanilla never touched the statement, or touched it at or before your baseline | info |

A conflict (both changed, removed changed) is **mid**: your copy replaces vanilla's block, so your statement applies before and after vanilla's change and the game behaves as it did. Any other vanilla change is **high** when the block holding it also holds an edit of yours, a conflict included, since the two compete, and **mid** otherwise. A copy still holding an old vanilla value, or a statement vanilla deleted, is flagged whatever its baseline, since that is vanilla's text and not an edit; so is vanilla's deleted text inside a block only your copy has. A block of your own wrapped around vanilla's statements moves them to a different place, so they are not linked to vanilla's history there.

For example, suppose vanilla's history of a block reads:

```
1.0:   cost = 100
1.1:   cost = 200   upkeep = 5
```

and your copy reads `cost = 100` plus `custom = yes`. The baseline is 1.0. `cost` is a vanilla change (you still hold 1.0's value), `upkeep` is a vanilla addition, and `custom` is your edit. Because your edit sits in the same block, both vanilla changes are high.

### Findings

Every high or mid change is one finding of kind `<audit>_<change>_<priority>`, such as `override_vanilla_added_mid` or `gui_vanilla_changed_high`. In a GUI copy, the changes vanilla made inside one block are one finding instead: a change joins the outermost block above it in an unbroken run of blocks that each hold a change of their own, and a block holding several changes is one `gui_block_changed_<priority>` finding, high when any of its changes is high. High is **stale** and mid is **review** in the summary. Your edits are counted only as the absence of findings.

A finding records its location (the line of your statement, or for a statement only vanilla has, the line it follows in your copy), `since` (the patch in which vanilla made the change) and `base` (the version your copy matches). Its key holds the target, the enclosing keys, and both sides' text with layout collapsed, so a dismissal holds until either side's text changes (section 10). For a change that meets an edit of yours (both changed, removed changed), the key also holds vanilla's statement before the change as `was`, which the id ignores, and the summary and detail show it: `yours: price = 4`, `vanilla: price = 8 → price = 6 (1.2.0)`.

History that starts after your copy was made cannot tell your edits from vanilla's earlier ones: a difference older than the oldest tracked version reads as yours.

---

## 5. Override audit

For each unique `INJECT`, `REPLACE`, `TRY_*` and `*_OR_CREATE` directive, the audit reads vanilla's top-level block of the same name in the same folder.

**REPLACE blocks** (`REPLACE`, `TRY_REPLACE`, `REPLACE_OR_CREATE`). The block is read at every snapshot in the window (section 2):

| vanilla's block | meaning | reported as |
|-----------------|---------|-------------|
| in the newest snapshot | compared with its history | one finding per change (section 4) |
| in an earlier snapshot only | vanilla removed the target | **orphaned override** (broken), with the patch that removed it |
| in no snapshot | not a top-level block | single value, nested name, or absent (below) |

A REPLACE whose braces never close cannot be read and is reported as broken.

**Single-value REPLACEs.** `REPLACE:levy_size = 0.02` has no block. The audit reads vanilla's top-level `name = value` statement at every snapshot in the window and compares `levy_size = 0.02` with them the same way as a block. Names that exist in vanilla only nested somewhere are reported informationally as a matcher limit; names that exist nowhere are reported as absent.

**INJECT.** An INJECT adds direct children to vanilla's block, so only vanilla's top-level children can collide with it. The audit compares vanilla's block at the window's old version, or at the base of an open INJECT finding for the target (section 10), with the new version. Blocks that read the same statement for statement are unchanged. When vanilla added, removed or changed a top-level key that the INJECT also adds, it is stale, since that key's final value in the game is no longer what it was; otherwise the injection still lands the same way and the change is informational. A target present at the old version and gone at the new one is orphaned.

**TRY_ and _OR_CREATE directives.** A target of either absent from every version is listed as expected: a `TRY_` override is then ignored, and an `_OR_CREATE` override creates the object. A `TRY_` target that vanilla removed is orphaned like any other. An `_OR_CREATE` target that vanilla removed is not orphaned, since the override now creates the object; it is reported for review.

---

## 6. Dependency audit

Run with `--deps`. This audit does not look at blocks; it looks at the names your script uses.

Paradox script is built from `key = value` statements, and this audit reads both the key on the left and, when the value on the right is a single name, that value too. It builds vanilla's vocabulary (every `name =` key in vanilla's script) at each tracked version, cached per snapshot, then flags any name your mod uses that vanilla used at some earlier version but does not use at the new one. Each finding names the patch that dropped it: the version after the last one where vanilla still used it.

Looking across every tracked version, not just the last patch, means a rename you missed once keeps being reported until you fix or dismiss it. With `--old`, `--full`, or a fixed window, only versions from the window's old version onward are considered.

Two kinds of finding are reported separately, because they mean different things. A dropped **key** is a statement you write that vanilla no longer uses, such as a modifier you set. A dropped **reference** is a name you point at as a value, such as a building in `has_building = building_farm`. Names the mod itself defines at the top level of its scripts are the mod's own and are never flagged.

The audit does not suggest renames. A guessed rename is noise; the finding tells you the name and the patch, and you look at what vanilla changed in that patch.

---

## 7. Localization audit

Run with `--loc`. Localization lives in `.yml` files under a module's `localization/` folder, as `KEY: "value"` lines under a language header (`l_english:`, `l_french:`, ...). A mod overrides a vanilla string by redefining the same key in any of its own `.yml` files. There is no same-path or same-file requirement, so the audit cannot key on filenames.

Two facts shape it:

- **The unit is `(language, key)`, not `key`.** The same key exists once per language with a different value. Matching on the bare key would compare English against French, so the language, read from the file's header, is part of the identity.
- **Matching is by name, never by file.** Vanilla renames loc files and moves keys between them constantly. The audit builds a global `(language, key) -> value` view and ignores which file a key lives in.

The mod's value of a key is its copy in a `replace/` folder when it has one (section 3), and otherwise its first copy. For each key the mod defines, the audit compares vanilla's value at the baseline and the new version:

| baseline | new | meaning | reported as |
|----------|-----|---------|-------------|
| present, present, same | vanilla left the string alone | unchanged |
| present, present, different | vanilla reworded or corrected the string | **changed** |
| present, absent | vanilla removed the key | orphaned override |
| absent, present | vanilla just added a key you also define | new collision |
| absent, absent | the key is yours, not an override | mod-only |

The baseline is the window's old version, except for a key with an open finding from an earlier run, which is measured from the version that finding was measured from (section 10). That is how a reworded string you have not dealt with stays reported after the next snapshot.

---

## 8. GUI audit

Run with `--gui`. GUI overrides are implicit (section 3), so this pass has two jobs. Both read vanilla's GUI files at every snapshot in the window (section 2).

### Path A: shadowed definitions

The mod defines a `template` or `type` whose name also exists in vanilla. The copy is the definition's text, and vanilla's history is its definition of the same name at each snapshot. Each copy vanilla still defines is compared with that history (section 4), giving one finding per change.

Two cases are also reported on their own:

- **New collision.** Vanilla added the name in the newest version. It is reported for that patch, and the copy is compared with vanilla's history like any other.
- **Removed from vanilla.** Vanilla defined the name earlier in the window and no longer does, so your copy is now the only definition.

### Path B: same-path file replacements

The mod ships a `.gui` file at the same path as a vanilla file, replacing it whole. The copy is the whole file, and vanilla's history is its file at each snapshot, compared statement by statement like a definition. Definitions inside such a file are audited here, not as shadows. A top-level definition (`template`, `type` or `types`) that vanilla's file has and your copy lacks, but that another of your `.gui` files in the same module defines, was moved there: it is left out of the file's comparison, at every version, and compared as a definition where it now lives. A file vanilla added in the newest version, or removed within the window, is also reported for review.

---

## 9. Baselines and attribution

A copy's **baseline** is the tracked version it differs from least. The distance to a version counts the statements that differ: each statement only one side has counts its size, and each lined-up pair that reads differently counts one (a pair of blocks, the statements differing inside). Among equally close versions the oldest wins, so a newer version is the baseline only when the copy fits it strictly better.

The baseline decides one question: whether vanilla made a change before or after the copy was taken.

- A change vanilla made **after** the baseline to a statement you also changed or deleted is a conflict (both changed, removed changed).
- A change vanilla made **at or before** the baseline was visible when the copy was taken, so a different value there is your edit.
- Vanilla's own text left in your copy (an old value, a deleted statement) is flagged whatever the baseline.

`since` is the version in which vanilla made the change: the version after the newest one that still had your value, or the version where vanilla's current statement first appeared in a block that already existed. History is read from the start of the text's latest unbroken run of versions, so a block vanilla removed and later re-added is measured from its return.

Because findings come from vanilla's whole history in the window rather than from the last patch, a change stays reported until you take it or dismiss it, however many snapshots follow. `--old` shortens the history: with a window of a single version there is nothing to attribute changes with, and every difference reads as yours.

---

## 10. Findings records

pdx-audit keeps a record per mod and commit in the per-user data folder, never inside the mod:

```
<data folder>/<mod id>/commits/<commit hash>.json
<data folder>/<mod id>/record.json      # a mod that is not in git
<data folder>/<mod id>/results.json     # the last run the --display app made
```

The mod id comes from `.metadata/metadata.json`; an id that is not a safe folder name is refused and records are disabled for that run.

A record holds two maps:

- `dismissed`: finding id → what was dismissed, when, and an optional reason;
- `open`: finding id → the finding, the patch it came from (`since`), the version it was measured from (`base`), and for a finding measured against a source other than vanilla, that source (`source`), with `since` and `base` as tags in that source's own order.

### Reading and writing

A run lists HEAD's first-parent history (`git rev-list --first-parent HEAD`) and loads the record of the first commit that has one. It writes the full merged state to HEAD's file only when something changed, via a temporary file renamed into place. Consequences:

- a new commit inherits its parent's record without a new file;
- a branch sees the decisions made up to where it split off, plus its own;
- merging keeps the first parent's decisions, so dismissals made only on a merged branch reappear once;
- separate clones of the same mod share storage: identical history means identical hashes, and diverging commits get their own files.

### Finding ids

A finding's id is the SHA-1 of its kind plus its **key**: the target (for example `override:in_game/common/building_types/some_building`) and, for a change to a copy, the enclosing keys plus your statement's text and vanilla's, each with layout collapsed. When the text compared belongs to a foundation or an adopted source, the key also holds that source's id as `base`; a unit vanilla owns keeps the same id whether or not the mod has sources. Line numbers, detail text and tracker hashes are never part of it. The summary shows the first 8 characters, which `--dismiss` accepts (an ambiguous prefix is an error).

Because the id is content, a dismissal applies only while the facts are the same. When vanilla changes its statement again or you change yours, the id changes and the finding comes back. Duplicates within the mod cannot be dismissed.

Two findings can have the same content, such as the same change in two identical rows of a copy, or in two copies of one GUI template or REPLACE. Those findings add their file to the key as `file`, and the ones in the same file also add their order there by line as `occurrence`, counting from 1. A finding whose content no other finding shares keeps its plain id, so edits elsewhere never change it. A finding's id changes when it gains its first match or loses its last one, and a finding's `occurrence` changes when a matching finding above it in the same file is added or removed.

### Open findings and carry-forward

Default runs (no `--old`, `--new`, `--full`, `--block`, `--category`, `--vanilla-only` or `--adopted`) replace the `open` map with this run's actionable, undismissed findings, keeping the `since` and `base` of any finding that was already open. Findings no longer produced are closed. A run of named audits replaces only the entries those audits produce, and keeps the rest; an entry measured against an adopted source belongs to the adopted pass. A dismissal in the audits a default run covered that the run did not produce is marked `gone`, which `--show-dismissed` and the Dismissed page show. On the next run, the base of an open localization finding feeds the localization audit (section 7), and the base of an open INJECT finding feeds the INJECT check (section 5); findings of other kinds on the same target never widen these checks, so a repeated run with nothing changed reports the same findings. Drift keeps being reported until it is fixed or dismissed, even after the window moves past its patch. REPLACE blocks and GUI copies read their whole history in the window (section 9), so their findings carry forward without it. The summary lists findings from before the newest patch under **Still open from earlier patches**.

### Orphaned records

A record whose commit is not reachable from any branch, remote branch, tag, or HEAD is orphaned. Each run prints a note naming the mod id and short hashes. `--remove-orphaned-records` re-checks, lists the full paths, asks for confirmation (refusing without a terminal unless `--force` is given), and removes each file through the removal helper (section 17), re-checking reachability first. Nothing is removed automatically, because a commit unknown to this clone may belong to another clone of the same mod.

---

## 11. Duplicate audit

Run with `--dupes`. The rule is one source of truth per definition: within a `common/<type>` folder, across all module roots, each name should be defined or overridden in exactly one place in the mod.

The audit scans top-level statements in every mod script and groups them by type and name, counting plain definitions and every prefixed override (`INJECT:`, `REPLACE:`, `TRY_*`, `*_OR_CREATE`) alike. It also groups localization entries and on_action keys:

| Finding | Case | Severity | Dismissible |
|---------|------|----------|-------------|
| multiple sources | 2+ entries of any mix for one name | broken | no |
| define key set twice | the same key inside a define namespace set in 2+ places | broken | no |
| GUI definition twice | the same template/type in 2+ mod `.gui` files | broken | no |
| loc key defined more than once | the same `(language, key)` 2+ times with differing text | broken | no |
| loc key defined more than once, same text | the same `(language, key)` 2+ times with identical text | review | no |
| on_action syntax | one on_action block setting `effect` or `trigger` twice | broken | no |
| on_action key conflict | one on_action's `effect` or `trigger` set in 2+ blocks | broken | no |
| plain in other file | a plain definition of a vanilla name in a file not at vanilla's path | review | yes |
| file override drops definitions | a mod file at vanilla's path that lacks some of vanilla's definitions | informational | yes |

**Localization keys.** Every entry counts, repeats inside one file included. A mod's localization files form two groups, the files under a `replace/` folder and all others, and a key is a duplicate when it appears twice within one group. A `replace/` copy overriding the same key in the other group is intended and not reported. A finding lists every copy and names no winner, since the remedy is to keep one; with identical text it is review, since the extra copies are only dead weight.

**Merging types.** Some types are merged by the engine across files; vanilla itself defines `on_game_start` in several on_action files and `NGame` in several define files. The audit works these out from the newest snapshot, as every type in which vanilla defines some name in more than one file, and exempts them from the multiple-sources and plain-in-other-file checks. The config key `merge_types` adds more. For `defines` the audit checks one level deeper, flagging the same key set twice. For on_actions it checks the two keys the engine holds once: a second `effect` or `trigger` in one block, and the same on_action's `effect` or `trigger` in two blocks of the mod. Other on_action keys combine and are not reported.

Classes of these findings list one line per mod file in the summary. Only the newest snapshot is needed, and its definition index is cached. Duplicate event ids are left to the engine, which logs them.

---

## 12. Sources

Besides vanilla, a mod can be compared with two kinds of source, both chosen by you and stored per mod:

- **Foundations**: dependencies that load before the mod, in the order you store. Vanilla and the foundations are flattened into one base (section 13), and the mod is compared against it (section 14).
- **Adopted sources**: mods whose code the mod absorbed, fully or partly. The mod's copies are compared with each source's own history (section 15).

pdx-audit never writes into a mod or a source folder. Everything it keeps about sources lives in the per-user data folder:

```
<data folder>/<mod id>/sources.json     the mod's chosen sources
<data folder>/sources/registry.json     storage key -> source id and folder
<data folder>/sources/<key>.json        a source's patch assignments and snapshot facts
<data folder>/sources/repo.git          the tracker holding folder sources' snapshots
<data folder>/sources/scan.json         the cached suggestion scan
<data folder>/sources/cache/            parsed indexes of source versions
<data folder>/stacks/<hash>/cache/      flattened overlays of a stack's points
```

Every action exists both as a command-line option and on the app's Sources page, and both call the same function in `pdxaudit/sources.py`. The app re-reads the stored files when its window is activated, so a change made on the command line shows without a restart.

### Kinds, ids and paths

A source is the vanilla tracker, a **git** source or a **folder** source. A folder inside a Steam workshop content folder is a folder source even when it ships a `.git`, since Steam replaces its files without updating that repository; any other folder whose top holds a working `.git` is a git source; everything else is a folder source. A kind you store overrides the detection.

A source's id is its metadata id, or its folder name when it has none. Ids are unique within one mod's choices, since finding keys, records and the source options name a source by id; adding a folder whose id is already chosen is refused unless you replace the chosen one.

Every path is canonicalized before it is stored or compared: made absolute with symlinks resolved, with a Windows drive path (`C:\x`) turned into `/mnt/c/x` under WSL and back on Windows. On Windows drives and their `/mnt/<drive>` mounts, paths compare case-insensitively while the stored path keeps its spelling.

### Storage keys

`registry.json` maps each storage key to a source id and a canonical path. A key is the sanitized id plus a random six-character hex suffix, assigned when a folder is first added by any mod. Adding a folder looks up the registry by path first, so every mod choosing the same folder shares its snapshots and patch assignments, while a development clone and a published copy with the same id stay apart.

A folder that moved keeps its key through relocation (`--relocate-source`, or **Locate** on the Sources page), which updates the stored path; a folder whose metadata id differs is refused. Adding a folder whose id matches a stored source whose folder is gone names the relocation instead of guessing; nothing is relocated without your choice. A chosen source whose folder is gone is warned about on each run and left out until it is relocated or removed.

### Versions

Each source lists its versions in its own history order, never by date:

- **vanilla**: the tracker's commits, oldest first;
- **git**: along the repository's first-parent history, every commit where `.metadata/metadata.json` changes `version`, tagged with that version, then HEAD, tagged with its short hash when it is not one of them. The repository is only read;
- **folder**: the snapshots taken on request (`--snapshot-source`). A snapshot hashes the folder's script, GUI and localization files and its metadata file straight into the shared tracker, with bytes stored exactly, under the refs `refs/sources/<key>/head` and `refs/sources/<key>/tags/<tag>`. The tag is the metadata version, suffixed `.2`, `.3` when the content changed without a version bump. A folder that ships a `.git` has that repository's first-parent history imported under its refs on the first snapshot. Identical files across sources are stored once, and a folder whose files match its newest snapshot apart from carriage returns records nothing. A source's texts are read with Windows line endings turned into Unix ones, as the mod's own files are.

Every write to the shared tracker (a snapshot, a history import, orphan removal) holds `tracker.lock`, created exclusively with the writer's process id and start time. A second writer waits, then fails naming the holder; a lock whose process is gone is stale and is removed before retrying.

### Patches

A foundation version is used only once it belongs to a vanilla patch, stored in the source's `<key>.json` with whether it was defaulted or chosen.

- When pdx-audit records a new version, it gets the newest tracked patch, raised to the patch of the nearest earlier version with one when that is newer. When that earlier patch is not in the current tracker, the new version is left without a patch and a warning says so. A default given while the tracker is out of date is warned about and marked.
- Versions that existed before the source was added have no patch; the source's newest version at that time gets the default.
- You assign patches one version at a time or to a run of consecutive versions (`--patch <id> <version>[..<version>] <patch>`). Patches never decrease along a source's history, so an assignment that would break this is refused and names the conflicting version.
- Only foundations take patches; an adopted source is compared with all of its versions.

### Freshness

- A Steam workshop folder is flagged when its manifest in `appworkshop_<appid>.acf` differs from the one recorded at its newest snapshot. This check runs on every run.
- A folder source's files are also compared with its newest snapshot, ignoring carriage returns: by `--sources`, and in the app on a background thread after a run.
- A git source is flagged when HEAD moved past its newest recorded version, and, by `--sources`, when its working tree differs from HEAD ignoring carriage returns.

### Suggestions

pdx-audit scans candidate folders and reads each one's metadata: the children of the local mod folder, the workshop content folder of every Steam library holding the game (`libraryfolders.vdf` lists the libraries; the app id comes from the game's `appmanifest_<appid>.acf`), and the folders in the launcher's `playsets.json`, which is read only as a list of installed folders. A folder is suggested when its id matches a dependency the mod declares; while such a dependency is not chosen, every local git repository whose id matches no dependency is suggested beside them, so a development clone with a differing id can stand in. Nothing is added without your choice, and a dismissed suggestion stays hidden until the mod's declared dependencies change.

Workshop folders are named by item number, not by id, so matching ids means reading every candidate's metadata. The result is cached in `scan.json`, stamped with the modification time and size of `libraryfolders.vdf`, each library's `appworkshop_<appid>.acf`, the local mod folder, each local candidate's metadata file (a missing one stamped as absent), and `playsets.json`. A run never scans: it uses the cache while every stamp still matches, which costs one stat per stamp, and otherwise prints a one-line note to run `--sources`. `--sources` and opening the Sources page refresh the cache at once, and the app refreshes it in the background after a run.

### Orphaned sources

A registry key no mod's `sources.json` references is orphaned, and each run prints a note naming it. `--remove-orphaned-sources` lists them and asks for confirmation (refusing without a terminal unless `--force` is given). Holding the tracker lock, for each key it deletes the key's refs with `git update-ref -d`, removes `<key>.json` and the key's cache files through the removal helper, and drops the registry entry. It then runs `git gc` with git's default prune expiry, so objects a concurrent or interrupted snapshot wrote before creating its refs are kept until they age out.

---

## 13. Flattening

A stack is vanilla followed by the mod's foundations in the stored order. Flattening resolves every unit a foundation touches, starting from vanilla's text and applying each foundation in turn by the rules in section 3:

- an exact path, plain definition, GUI template or type, or localization key from a later layer replaces the unit; within one foundation the alphanumerically first file name's copy is that foundation's, and for localization a `replace/` copy comes before the rest;
- REPLACE and TRY_REPLACE replace a present unit; REPLACE_OR_CREATE replaces or creates it;
- INJECT and TRY_INJECT add their insides to a present unit; INJECT_OR_CREATE adds them or creates the unit from them;
- a TRY_ directive on a missing unit does nothing, and a foundation's INJECT or REPLACE on a missing unit is skipped without a finding, since it is the foundation's to maintain;
- a merging type combines entries in load order, and within one foundation in file name order; in an on_action, a later layer's `effect` or `trigger` replaces the earlier one;
- a unit a foundation defines twice with no stated winner is flattened from its first copy, silently.

A unit is a script definition (folder and name), a GUI template or type (module, kind and name), a whole file (path) or a localization key (language and key). Each unit a foundation touches records its owner: the layer, version and file whose definition it is. A unit other layers injected into or merged entries into also lists every contributor in load order. A merged unit lists its entries in load order; since plain statements compare as a multiset and repeated blocks with the same key pair in order (section 4), a copy is measured against that load order.

### The stack's history

A stack's history is a list of **points**. The points are vanilla's versions in tracker order, with each patched foundation version inserted after the vanilla version matching its patch; several foundation versions on one patch follow the stored foundation order, then their own history order. At each point, each foundation is at its newest version placed so far, or absent before its first. Versions without a patch are never used, since combining a foundation's text with a vanilla version it was not made for produces text that never existed. A vanilla point's tag is its version tag; a foundation point's tag is `<source id> <version>`, which `--old` and `--new` also accept. Every point placed on the newest vanilla version counts as this patch in the summary.

The audits read the same indexes at a stack's points as at vanilla's snapshots: GUI definitions and files, top-level blocks, single values and names, vocabulary with each replaced vanilla file taken out, localization values, and the definitions the duplicate audit uses. Merging types come from vanilla's newest snapshot. `--source-version` limits a source to its versions up to the one named.

---

## 14. The mod against its foundations

A run of a mod with a chosen foundation compares the mod against its stack; `--vanilla-only` compares it against vanilla alone, as a mod without foundations is. Every audit works the same way against a stack as against vanilla, with each point standing in for a snapshot, so a change a foundation made is attributed and prioritized exactly like a change vanilla made: your own edits are info, conflicts and base changes in a block that also holds an edit of yours are high, and other base changes are mid. A statement a foundation added that your copy lacks is a base statement like any other.

In the per-audit detail, each finding also says whose content it touches: **changes vanilla content**, **changes foundation content**, or **drops a foundation addition**. The label is display only; it changes neither priority nor id. The app marks the gutter of a line whose change a foundation made.

**Duplication.** A mod unit identical, by statement distance 0, to a flattened unit a foundation owns is a `foundation_duplicate` finding (review): the foundation already provides it. This covers GUI definitions and files, REPLACE blocks, plain script definitions and localization keys. A mod unit that differs from such a unit is an override of the foundation's, compared with the stack's history, and its findings carry the foundation's id as `base`.

pdx-audit reports only what the mod does. A foundation's own state, such as a foundation behind a newer vanilla change, a foundation's duplicate units, or a foundation's directive on a missing unit, is never reported.

---

## 15. Adopted sources

For each unit the mod defines that exists anywhere in an adopted source's history, pdx-audit compares the mod's copy with the source's texts of it in the source's own history order (section 12), by the method in section 4. The copy's baseline is the source version it differs from least, the oldest on ties, and every change the source made after it is a finding of today's kinds (`gui_<change>_<priority>`, `override_<change>_<priority>`, or `loc_changed` for a localization key), with the source's id as `base` in its key. The summary, the detail and the app word these findings with the source's name in place of vanilla's, label its side of each change `upstream`, and never list them as earlier patches, since their tags are the source's.

**Matching.** A whole file matches by path, and the definitions inside a file matched that way are compared with the file. GUI definitions, script definitions and localization keys match by name. Script blocks match within their kind: an INJECT, or a plain entry of a merging type such as an on_action, which the engine merges in rather than defines, matches either of those; a definition or REPLACE matches a definition or REPLACE. A unit vanilla defines stays vanilla's when a source stops overriding it, so it is neither compared nor reported once the source's newest version no longer has it.

**Rename rules.** A rule `from` → `to`, set per adopted source, rewrites identifiers and file names on the source side that start with `from`, before matching. It never applies to a name vanilla uses: a key in vanilla's vocabulary or a vanilla script definition.

**Findings besides changes.**

| Kind | Severity | Case |
|---|---|---|
| `adopted_unit_removed` | stale | the source deleted a unit the mod still carries |
| `adopted_unit_added` | review | the source added a unit to a file the mod carries units from, and the mod does not define it |

An added unit's key holds a hash of its text, so a dismissal lasts until the source changes that unit. Files the mod carries nothing from are not reported. These classes list one line per mod file in the summary.

**An adopted source over a foundation.** When an adopted source declares a dependency that is also one of the mod's foundations, its own layer is worked out first: each unit it defines is classified against the flattened foundations' history as its own new name, a redefinition identical to the flattened foundations at some point, or a redefinition with changes. Only its new names and changed redefinitions are compared as adopted; the rest belong to the foundations, and the mod's copies of them are reported as duplicates of the foundation (section 14).

**One target, several bases.** A unit compared against vanilla or the stack and against an adopted source is listed once in the summary, under **Compared with more than one base**, with each base's findings under it. When the adopted source's newest version already contains a vanilla change the mod lacks, the group shows one remedy: take that source version.

A default run of a mod with adopted sources includes this pass; `--adopted <id>` runs it alone for one source, and `--vanilla-only`, `--block`, `--category` and runs naming audits leave it out.

---

## 16. Caching

The override and GUI audits read parsed indexes at every snapshot in their window, not just two, so each is cached on disk, keyed by the commit's full hash: block indexes, GUI indexes, vocabularies, and the duplicate audit's definition index. Vanilla's caches sit under `<vanilla-tracker>/cache/`; a source's sit under `<data folder>/sources/cache/`, with the source's storage key in each file name.

- A commit's content is immutable, so a cache entry for a given hash is never stale.
- A version number in the file name (`gui-v1-...`) lets a parser change retire old entries.
- Entries for commits no longer in their source are pruned at the start of each run, per source, one validated file at a time.

A stack's points are cached as overlays on vanilla's indexes: what the foundations add, change or remove at that point, with each unit's owner. They sit under `<data folder>/stacks/<stack hash>/cache/`, where the stack hash covers the foundations and their order, and each file is named by its point, whose id covers the member commits and so the patch assignments. An overlay is pruned when its point leaves the stack, and every overlay of a stack no mod's foundations make up any more is pruned too.

The first run after a new snapshot pays to read that snapshot once; every run after is served from cache.

Within a single run, the audits also share their work. The mod's module-root folders are listed once, each mod file is read once, each commit hash is resolved once, each source version's files are read once, and each distinct text is parsed once, no matter how many audits or snapshots use it. The localization audit is not cached on disk. It reads only the vanilla `.yml` files in the languages the mod defines, in tree order so the first file defining a key still wins, and parses each distinct file once, so the old and new versions share every file vanilla left unchanged. Nothing is shared between runs, so edits to the mod between runs are always seen.

---

## 17. Deletion safety

pdx-audit deletes single files in these situations: a stale snapshot index in the vanilla tracker or the shared source tracker, cache entries and stack overlays for commits or points that are gone, orphaned records and the files of orphaned sources on request, a stale tracker lock, and a temporary file left by an interrupted write. All of them go through one function, `pdxaudit.safety.remove_file`, which removes a single file only when:

- its name fully matches the expected pattern (for a record, 40 hex characters plus `.json`);
- after resolving the path, it sits directly inside the expected folder, and that folder is not a filesystem root;
- it is a regular file, not a symlink or a directory.

Anything else raises and leaves the target alone, so a malformed or hallucinated path cannot remove a directory tree. pdx-audit never removes folders, even empty ones. An orphaned source's snapshots are dropped by deleting its git refs; git removes the unreferenced objects itself, after its default expiry.

`tests/test_no_recursive_removal.py` enforces this. It parses every source file with Python's `ast` module and fails on `shutil.rmtree`, `os.removedirs`, `os.rmdir`, `Path.rmdir`, `tempfile.TemporaryDirectory`, `os.system`, subprocess calls to `rm`-style commands, and any `unlink`/`os.remove` outside the helper. It is a tripwire for ordinary code, not a sandbox.

---

## 18. Version window reference

| Invocation | Versions for REPLACE blocks and GUI copies | Window for the other audits | Records updated |
|------------|--------------------------------------------|-----------------------------|-----------------|
| `pdx-audit` | every snapshot (or stack point) up to the newest | the last patch, or an open finding's base | yes |
| `pdx-audit --gui` (any named audits) | as the default | as the default | those audits' open findings |
| `pdx-audit --full` | every snapshot up to the newest | the oldest snapshot to the newest | dismissals only |
| `pdx-audit --new Y` | every snapshot up to Y | the snapshot before Y to Y | dismissals only |
| `pdx-audit --old X --new Y` | X through Y | X to Y | dismissals only |
| `pdx-audit --block B` / `--category C` | as the default | as the default | dismissals only |
| `pdx-audit --vanilla-only` | as the default, against vanilla alone | as the default, against vanilla alone | dismissals only |
| `pdx-audit --adopted S` | S's versions | none; only the adopted pass runs | dismissals only |

In a stack, `--old` names the vanilla point of that version and `--new` reaches the newest point placed on that version. Dismissals always apply; open findings are only rewritten by default runs, and only for the audits a run covers, so a filtered or fixed-window run never closes findings it did not look at. A change the localization audit or the INJECT check finds across more than one version is dated by the version that made it. `--full` and `--old` cannot be combined.

---

*This document describes internal behavior and may lag the code. When in doubt, the code is the source of truth.*
