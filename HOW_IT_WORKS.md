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
12. [Caching](#12-caching)
13. [Deletion safety](#13-deletion-safety)
14. [Version window reference](#14-version-window-reference)

---

## 1. The problem being solved

A mod overrides pieces of the base game. When the game patches, the base game moves and the mod's copies do not. Several things can go wrong:

- A block you replaced wholesale gains new lines or new values in vanilla that your replacement now silently drops.
- A block you injected into moves or disappears, so your injection lands nowhere.
- A modifier or trigger you reference gets renamed or removed, so your reference is dead.
- The same definition ends up overridden from two places in the mod, so which one wins depends on load order.

None of these produce an error at load time. The mod just quietly does the wrong thing. pdx-audit exists to make that drift visible before it ships, without re-reporting decisions you already made.

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
REPLACE:some_block = { ... }       # replace vanilla's block entirely
REPLACE:some_value = 5             # replace a single-value definition
INJECT:some_block = { ... }        # add to / modify vanilla's block
TRY_INJECT:maybe_block = { ... }   # inject only if the target exists
```

In GUI files the override is implicit: if the mod defines a `template` or `type` with the same name as a vanilla one, or ships a `.gui` file at the same path as a vanilla file, it overrides it. There is no keyword; sameness of name or path *is* the override. This is why the GUI audit is a separate pass with its own logic (section 8).

---

## 4. Comparing a copy with vanilla's history

A REPLACE block, a shadowed GUI definition and a replaced GUI file are all a **copy**: vanilla text taken at some game version and then edited. For each difference between your copy and vanilla's newest text, pdx-audit works out **who made it**.

### Lining up statements

The copy and vanilla's newest text are parsed into statements: `key = value`, a block, or a run of bare values such as `0.0 0.0 0.0 1.0`. Sibling statements pair in order, trying in turn:

1. the same statement;
2. the same key (a block that sets `name = "..."` is known by that name first, then by its key alone);
3. the same distinctive quoted value under another key, such as vanilla moving `onpressed = "[OnPause]"` to `on_action = "[OnPause]"`.

What is still unpaired pairs by key out of order, so a moved block still pairs. Paired blocks are compared the same way inside. Layout, comments and the spelling of numbers (`0.10` versus `0.1`) never count as a difference. Repeated statements in one block, such as list members, compare as a multiset, so their order never matters. A REPLACE block is compared by its contents, since its own key (`REPLACE:some_block`) differs from vanilla's.

### Attributing each difference

Each difference is looked up in vanilla's tracked history at the same place: the keys of the enclosing blocks plus the statement's own key. The copy's **baseline** (section 9) decides whether vanilla made a change before or after the copy was taken.

| Change | Meaning | Priority |
|--------|---------|----------|
| vanilla changed | your value is one vanilla had at this place; vanilla has changed it | high or mid |
| vanilla added | vanilla added the statement after the block existed; your copy lacks it | high or mid |
| vanilla removed | your copy carries a statement vanilla had here and has deleted | high or mid |
| both changed | vanilla changed a statement you changed too, or deleted one you changed, after your baseline | high |
| removed changed | vanilla changed a statement you deleted, after your baseline | high |
| your edit | vanilla never touched the statement, or touched it at or before your baseline | info |

A vanilla change is **high** when the block holding it also holds an edit of yours, since the two compete, and **mid** otherwise. A copy still holding an old vanilla value, or a statement vanilla deleted, is flagged whatever its baseline, since that is vanilla's text and not an edit; so is vanilla's deleted text inside a block only your copy has. A block of your own wrapped around vanilla's statements moves them to a different place, so they are not linked to vanilla's history there.

For example, suppose vanilla's history of a block reads:

```
1.0:   cost = 100
1.1:   cost = 200   upkeep = 5
```

and your copy reads `cost = 100` plus `custom = yes`. The baseline is 1.0. `cost` is a vanilla change (you still hold 1.0's value), `upkeep` is a vanilla addition, and `custom` is your edit. Because your edit sits in the same block, both vanilla changes are high.

### Findings

Every high or mid change is one finding of kind `<audit>_<change>_<priority>`, such as `override_vanilla_added_mid` or `gui_both_changed_high`. In a GUI copy, the changes vanilla made inside one block are one finding instead: a change joins the outermost block above it in an unbroken run of blocks that each hold a change of their own, and a block holding several changes is one `gui_block_changed_<priority>` finding, high when any of its changes is high. High is **stale** and mid is **review** in the summary. Your edits are counted only as the absence of findings.

A finding records its location (the line of your statement, or for a statement only vanilla has, the line it follows in your copy), `since` (the patch in which vanilla made the change) and `base` (the version your copy matches). Its key holds the target, the enclosing keys, and both sides' text with layout collapsed, so a dismissal holds until either side's text changes (section 10).

History that starts after your copy was made cannot tell your edits from vanilla's earlier ones: a difference older than the oldest tracked version reads as yours.

---

## 5. Override audit

For each unique `INJECT`/`REPLACE`/`TRY_*` directive, the audit reads vanilla's top-level block of the same name in the same folder.

**REPLACE blocks.** The block is read at every snapshot in the window (section 2):

| vanilla's block | meaning | reported as |
|-----------------|---------|-------------|
| in the newest snapshot | compared with its history | one finding per change (section 4) |
| in an earlier snapshot only | vanilla removed the target | **orphaned override** (broken), with the patch that removed it |
| in no snapshot | not a top-level block | single value, nested name, or absent (below) |

A REPLACE whose braces never close cannot be read and is reported as broken.

**Single-value REPLACEs.** `REPLACE:levy_size = 0.02` has no block. The audit reads vanilla's top-level `name = value` statement at every snapshot in the window and compares `levy_size = 0.02` with them the same way as a block. Names that exist in vanilla only nested somewhere are reported informationally as a matcher limit; names that exist nowhere are reported as absent.

**INJECT.** An INJECT adds direct children to vanilla's block, so only vanilla's top-level children can collide with it. The audit compares vanilla's block at the window's old version, or at the base of an open finding for the target (section 10), with the new version. Blocks that read the same statement for statement are unchanged. When vanilla added, removed or changed a top-level key that the INJECT also adds, it is reported for review; otherwise the injection still lands the same way and the change is informational. A target present at the old version and gone at the new one is orphaned.

**TRY_ directives.** A `TRY_` target absent from every version is listed as expected. A target that existed and vanilla removed is orphaned like any other.

---

## 6. Dependency audit

Run with `--deps`. This audit does not look at blocks; it looks at the names your script uses.

Paradox script is built from `key = value` statements, and this audit reads both the key on the left and, when the value on the right is a single name, that value too. It builds vanilla's vocabulary (every `name =` key in vanilla's script) at each tracked version, cached per snapshot, then flags any name your mod uses that vanilla used at some earlier version but does not use at the new one. Each finding names the patch that dropped it: the version after the last one where vanilla still used it.

Looking across every tracked version, not just the last patch, means a rename you missed once keeps being reported until you fix or dismiss it. With `--old`, `--full`, or a fixed window, only versions from the window's old version onward are considered.

Two kinds of finding are reported separately, because they mean different things. A dropped **key** is a statement you write that vanilla no longer uses, such as a modifier you set. A dropped **reference** is a name you point at as a value, such as a building in `has_building = building_farm`. Names the mod itself defines at the top level of its scripts are the mod's own and are never flagged.

The audit does not suggest renames. A guessed rename is noise; the finding tells you the name and the patch, and you look at what vanilla changed in that patch.

---

## 7. Localization audit

Run with `--loc`. Localization lives in `.yml` files as `KEY: "value"` lines under a language header (`l_english:`, `l_french:`, ...). A mod overrides a vanilla string by redefining the same key in any of its own `.yml` files; load order decides the winner. There is no same-path or same-file requirement, so the audit cannot key on filenames.

Two facts shape it:

- **The unit is `(language, key)`, not `key`.** The same key exists once per language with a different value. Matching on the bare key would compare English against French, so the language, read from the file's header, is part of the identity.
- **Matching is by name, never by file.** Vanilla renames loc files and moves keys between them constantly. The audit builds a global `(language, key) -> value` view and ignores which file a key lives in.

For each key the mod defines, it compares vanilla's value at the baseline and the new version:

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

The mod ships a `.gui` file at the same path as a vanilla file, replacing it whole. The copy is the whole file, and vanilla's history is its file at each snapshot, compared statement by statement like a definition. Definitions inside such a file are audited here, not as shadows. A file vanilla added in the newest version, or removed within the window, is also reported for review.

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
- `open`: finding id → the finding, the patch it came from (`since`) and the version it was measured from (`base`).

### Reading and writing

A run lists HEAD's first-parent history (`git rev-list --first-parent HEAD`) and loads the record of the first commit that has one. It writes the full merged state to HEAD's file only when something changed, via a temporary file renamed into place. Consequences:

- a new commit inherits its parent's record without a new file;
- a branch sees the decisions made up to where it split off, plus its own;
- merging keeps the first parent's decisions, so dismissals made only on a merged branch reappear once;
- separate clones of the same mod share storage: identical history means identical hashes, and diverging commits get their own files.

### Finding ids

A finding's id is the SHA-1 of its kind plus its **key**: the target (for example `override:in_game/common/building_types/some_building`) and, for a change to a copy, the enclosing keys plus your statement's text and vanilla's, each with layout collapsed. Line numbers, detail text and tracker hashes are never part of it. The summary shows the first 8 characters, which `--dismiss` accepts (an ambiguous prefix is an error).

Because the id is content, a dismissal applies only while the facts are the same. When vanilla changes its statement again or you change yours, the id changes and the finding comes back. Duplicate definitions cannot be dismissed.

### Open findings and carry-forward

Default runs (no `--old`, `--new`, `--full`, `--block` or `--category`) replace the `open` map with this run's actionable, undismissed findings, keeping the `since` and `base` of any finding that was already open. Findings no longer produced are closed. On the next run, the base of an open finding feeds the localization audit (section 7) and the INJECT check (section 5), so drift keeps being reported until it is fixed or dismissed, even after the window moves past its patch. REPLACE blocks and GUI copies read vanilla's whole history in the window (section 9), so their findings carry forward without it. The summary lists findings from before the newest patch under **Still open from earlier patches**.

### Orphaned records

A record whose commit is not reachable from any branch, remote branch, tag, or HEAD is orphaned. Each run prints a note naming the mod id and short hashes. `--remove-orphaned-records` re-checks, lists the full paths, asks for confirmation (refusing without a terminal unless `--force` is given), and removes each file through the removal helper (section 13), re-checking reachability first. Nothing is removed automatically, because a commit unknown to this clone may belong to another clone of the same mod.

---

## 11. Duplicate audit

Run with `--dupes`. The rule is one source of truth per definition: within a `common/<type>` folder, across all module roots, each name should be defined or overridden in exactly one place in the mod.

The audit scans top-level statements in every mod script and groups them by type and name, counting plain definitions and every prefixed override (`INJECT:`, `REPLACE:`, `TRY_*`) alike:

| Finding | Case | Severity | Dismissible |
|---------|------|----------|-------------|
| multiple sources | 2+ entries of any mix for one name | broken | no |
| define key set twice | the same key inside a define namespace set in 2+ places | broken | no |
| GUI definition twice | the same template/type in 2+ mod `.gui` files | broken | no |
| plain in other file | a plain definition of a vanilla name in a file not at vanilla's path | review | yes |
| file override drops definitions | a mod file at vanilla's path that lacks some of vanilla's definitions | informational | yes |

**Merging types.** Some types are merged by the engine across files; vanilla itself defines `on_game_start` in several on_action files and `NGame` in several define files. The audit works these out from the newest snapshot, as every type in which vanilla defines some name in more than one file, and exempts them from the multiple-sources and plain-in-other-file checks. The config key `merge_types` adds more. For `defines` the audit checks one level deeper, flagging the same key set twice. Key-level checks for `on_action` are not done, because how the engine combines two `effect` or `trigger` blocks is unconfirmed.

Only the newest snapshot is needed, and its definition index is cached. Duplicate localization keys and event ids are left to the engine, which logs them.

---

## 12. Caching

The override and GUI audits read parsed indexes at every snapshot in their window, not just two, so each is cached on disk under `<vanilla-tracker>/cache/`, keyed by the commit's full hash: block indexes, GUI indexes, vocabularies, and the duplicate audit's definition index.

- A commit's content is immutable, so a cache entry for a given hash is never stale.
- A version number in the file name (`gui-v1-...`) lets a parser change retire old entries.
- Entries for commits no longer in the tracker are pruned at the start of each run, one validated file at a time.

The first run after a new snapshot pays to read that snapshot once; every run after is served from cache.

Within a single run, the audits also share their work. The mod's module-root folders are listed once, each mod file is read once, each commit hash is resolved once, and each distinct vanilla text is parsed once, no matter how many audits or snapshots use it. The localization audit is not cached on disk. It reads only the vanilla `.yml` files in the languages the mod defines, in tree order so the first file defining a key still wins, and parses each distinct file once, so the old and new versions share every file vanilla left unchanged. Nothing is shared between runs, so edits to the mod between runs are always seen.

---

## 13. Deletion safety

pdx-audit deletes files in exactly three situations: a stale snapshot index, cache entries for commits that left the tracker, and orphaned records on request. All three go through one function, `pdxaudit.safety.remove_file`, which removes a single file only when:

- its name fully matches the expected pattern (for a record, 40 hex characters plus `.json`);
- after resolving the path, it sits directly inside the expected folder, and that folder is not a filesystem root;
- it is a regular file, not a symlink or a directory.

Anything else raises and leaves the target alone, so a malformed or hallucinated path cannot remove a directory tree. pdx-audit never removes folders, even empty ones.

`tests/test_no_recursive_removal.py` enforces this. It parses every source file with Python's `ast` module and fails on `shutil.rmtree`, `os.removedirs`, `os.rmdir`, `Path.rmdir`, `tempfile.TemporaryDirectory`, `os.system`, subprocess calls to `rm`-style commands, and any `unlink`/`os.remove` outside the helper. It is a tripwire for ordinary code, not a sandbox.

---

## 14. Version window reference

| Invocation | Versions for REPLACE blocks and GUI copies | Window for the other audits | Records updated |
|------------|--------------------------------------------|-----------------------------|-----------------|
| `pdx-audit` | every snapshot up to the newest | the last patch, or an open finding's base | yes |
| `pdx-audit --full` | every snapshot up to the newest | the oldest snapshot to the newest | dismissals only |
| `pdx-audit --old X --new Y` | X through Y | X to Y | dismissals only |
| `pdx-audit --block B` / `--category C` | as the default | as the default | dismissals only |

Dismissals always apply; open findings are only rewritten by default runs, so a filtered or fixed-window run never closes findings it did not look at.

---

*This document describes internal behavior and may lag the code. When in doubt, the code is the source of truth.*
