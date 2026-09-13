# How pdx-audit Works

A technical walkthrough of what the tool does behind the scenes. The README covers usage; this document covers mechanism. It assumes you have read the README and want to understand *why* the audit reports what it reports.

## Contents

1. [The problem being solved](#1-the-problem-being-solved)
2. [The vanilla tracker](#2-the-vanilla-tracker)
3. [The override surface](#3-the-override-surface)
4. [The three-way comparison](#4-the-three-way-comparison)
5. [Override audit](#5-override-audit)
6. [Dependency audit](#6-dependency-audit)
7. [Localization audit](#7-localization-audit)
8. [GUI audit](#8-gui-audit)
9. [Baselines and adoption](#9-baselines-and-adoption)
10. [Findings records](#10-findings-records)
11. [Duplicate audit](#11-duplicate-audit)
12. [Caching](#12-caching)
13. [Deletion safety](#13-deletion-safety)
14. [Baseline selection reference](#14-baseline-selection-reference)

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

- **old / new**: the audit window. By default `new` is the newest snapshot and `old` is the one before it (the last patch). `--old`, `--new` and `--full` move them; `--old` must be older than `--new`.
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

## 4. The three-way comparison

The naive way to flag drift is: "did vanilla's version change?" That fires whenever vanilla changed anything, even something your override already accounts for or deliberately does differently. For a REPLACE the right question is: **for each thing vanilla changed, what did your copy do with the same thing?**

pdx-audit parses three texts, vanilla before (the baseline, section 9), your copy, and vanilla after, into statements keyed by **(key path, key)**. `cost = 100` inside `modifier = { ... }` becomes the value `100` at key `cost` under path `modifier`. Because values are matched by key and path, position inside the block never matters, and formatting (indentation, brace packing, `0.10` versus `0.1`) is normalized away. Operators are part of the statement, so `gold > 100` and `religion ?= x` are compared like `cost = 100`.

Each vanilla change is then classified:

| Class | Vanilla before → after | Your copy | Severity |
|-------|------------------------|-----------|----------|
| frozen | `cost = 100` → `cost = 200` | `cost = 100` | stale |
| new vanilla line | (none) → `upkeep = 5` | no `upkeep` | stale |
| kept removed line | `legacy = 1` → (none) | `legacy = 1` | stale |
| both changed | `cost = 50` → `cost = 200` | `cost = 25` | review |
| commented out | (none) → `trade_income = 0.1` | `#trade_income = 0.1` | informational |
| key removed | `cost = 1` → `cost = 2` | no `cost` | informational |
| already merged | `cost = 100` → `cost = 200` | `cost = 200` | informational |
| unclassified | a changed value among repeated keys | | review |

The stale classes are what a REPLACE hides by accident: you never touched the value (frozen), you never saw the line (new), or you still carry something the game deleted without changing it. **Both changed** is where you customized the value and vanilla changed it too, so the report shows all three values and a human decides; your "half the cost" may now be an eighth. The informational classes are choices you already made and are counted, not listed.

Two structural rules keep the list short:

- **Repeated keys compare as a set.** List members such as `religion ?= a religion ?= b` or bare tokens `{ A B C }` are compared as multisets, so order never matters and a new member is one new line. A changed value among repeated keys cannot be matched to its old value and is unclassified.
- **Whole sub-blocks are one finding.** A sub-block vanilla added that your copy lacks entirely is one "new vanilla line" (`modifier = { … } (3 lines)`), not one per inner line. Likewise a deleted sub-block you still carry unchanged is one kept removed line.

### Containment, for GUI copies

GUI definitions are not key/value data in the same sense, so the GUI audit uses **containment**: diff vanilla's baseline against vanilla's new text, then report added statements your copy lacks (missing) and removed statements your copy still carries (kept). Both texts are read as statements with the parser the pdx tools share: each `key = value`, each block opening, and each run of bare values such as `color = { 0.0 0.0 0.0 1.0 }` is one entry however it is laid out. Line breaks, indentation, brace placement and comments never read as drift, and your own extra statements are ignored.

---

## 5. Override audit

For each unique `INJECT`/`REPLACE`/`TRY_*` directive:

1. Pick the baseline (section 9) and look up the target block there and at the new version.
2. Branch on what exists where:

| baseline | new | meaning | reported as |
|----------|-----|---------|-------------|
| yes | yes, same after normalization | vanilla left it alone | unchanged |
| yes | yes, different | vanilla changed it | three-way classification (REPLACE) or injection-point check (INJECT) |
| yes | no | vanilla removed the target | **orphaned override** (broken) |
| no | yes | vanilla added a block you already replace | three-way against an empty baseline |
| no | no | not a top-level block | single value, nested name, or absent (below) |

**Single-value REPLACEs.** `REPLACE:levy_size = 0.02` has no block. The audit reads vanilla's top-level `name = value` statements at the baseline and the new version and classifies the value like one statement: frozen, both changed, already merged, or kept removed. Names that exist in vanilla only nested somewhere are reported informationally as a matcher limit; names that exist nowhere are reported as absent.

**INJECT.** An INJECT adds direct children to vanilla's block, so only vanilla's top-level children can collide with it. When vanilla added, removed or changed a top-level key that the INJECT also adds, it is reported for review; otherwise the injection still lands the same way and the change is informational.

**TRY_ directives.** A `TRY_` target absent from both versions is listed as expected. A target that existed at the baseline and vanilla removed is orphaned like any other.

**Baselines for REPLACE blocks.** Adoption (section 9) is scored with the three-way classes rather than raw lines: a vanilla patch counts as adopted only where the block already merged it, commented the line out, or removed the key. A value you customized that vanilla also changed is not adoption, so the baseline never moves past it and the **both changed** finding keeps being reported until you dismiss it. REPLACE blocks get no partial-adoption findings.

Each finding records `since`, the first patch after the baseline in which vanilla's block changed, so the summary can separate this patch's work from older drift.

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

Run with `--gui`. GUI overrides are implicit (section 3), so this pass has two jobs.

### Path A: shadowed definitions

The mod defines a `template` or `type` whose name also exists in vanilla. The audit finds these by name, picks each copy's baseline (section 9), and runs containment (section 4) between the baseline and the new version. A copy missing vanilla's change is stale; one already carrying it is reconciled and only counted.

A stale definition whose vanilla text changed in the audited patch is reported as **changed in this patch**. One that is stale only because of an earlier patch after its baseline is reported as **still behind from an earlier patch**, with the patch it came from.

### Path B: same-path file replacements

The mod ships a `.gui` file at the same path as a vanilla file, replacing it whole. Here containment does not help: your file is rebuilt by design, so "is your file missing a line vanilla added?" is true for many lines on purpose. For a whole replaced file the question is textual: **did vanilla's version of this file change between the baseline and now?** That makes the baseline decisive, which is why adoption-based baselines matter most here. The report lists the definitions vanilla changed, added or removed inside the file.

---

## 9. Baselines and adoption

A copy's **baseline** is the vanilla version it was last synced to. Measuring from there reports only what vanilla changed afterwards, instead of everything since the oldest snapshot. For each REPLACE block, shadowed GUI definition and replaced GUI file, the baseline is chosen in this order:

1. **Recorded.** A version saved in the findings record for this target: `--stamp-fork-points` saves GUI fork points, and an open finding remembers the version it was measured from.
2. **Detected.** The newest vanilla patch the copy adopted, found by adoption (below).
3. **Window.** Otherwise, the audit window's old version.

### Adoption

Walk vanilla's text for the target from the oldest snapshot to the new version. At each patch where vanilla's normalized text changed, compare that patch against the copy:

- added lines the copy contains count as adopted;
- removed lines the copy no longer has count as adopted;
- brace-only lines are ignored, and a "removed" line still present elsewhere in vanilla's new text is a move, not a removal.

The score is adopted lines divided by changed lines. A patch scoring at least **0.5** is adopted, and the baseline is the newest adopted patch. This is robust to the things that broke the older "closest snapshot" guess: indentation and brace packing are normalized (GUI copies compare statements, as in section 4, so a vanilla patch that only re-lays-out a block is not a change), the copy's own extra lines do not count against any version, and a few deliberately omitted lines still leave the patch adopted.

Two refinements:

- **Small patches cannot set the baseline on their own.** A patch with fewer than 3 changed lines only moves the baseline when the patch before it was also adopted (or it is the first change), so a one-line coincidence does not pull the baseline forward.
- **Partial adoption is reported for GUI copies.** An adopted patch of at least 3 changed lines, at or before the baseline, that scored below **0.75** gets a review finding such as `1.3.0: 49/83 of vanilla's changed lines adopted`, because the lines the copy lacks from it are not measured again. REPLACE blocks score adoption with the three-way classes instead (section 5) and get no partial findings.

When no patch was adopted, the copy may have been rewritten beyond recognition, so the audit falls back to the window's old version rather than reporting vanilla's entire history.

---

## 10. Findings records

pdx-audit keeps a record per mod and commit in the per-user data folder, never inside the mod:

```
<data folder>/<mod id>/commits/<commit hash>.json
<data folder>/<mod id>/record.json      # a mod that is not in git
<data folder>/<mod id>/results.json     # the last run the --display app made
```

The mod id comes from `.metadata/metadata.json`; an id that is not a safe folder name is refused and records are disabled for that run.

A record holds three maps:

- `dismissed`: finding id → what was dismissed, when, and an optional reason;
- `open`: finding id → the finding, the patch it came from (`since`) and the version it was measured from (`base`);
- `reviewed_against`: target → the version saved by `--stamp-fork-points`.

### Reading and writing

A run lists HEAD's first-parent history (`git rev-list --first-parent HEAD`) and loads the record of the first commit that has one. It writes the full merged state to HEAD's file only when something changed, via a temporary file renamed into place. Consequences:

- a new commit inherits its parent's record without a new file;
- a branch sees the decisions made up to where it split off, plus its own;
- merging keeps the first parent's decisions, so dismissals made only on a merged branch reappear once;
- separate clones of the same mod share storage: identical history means identical hashes, and diverging commits get their own files.

### Finding ids

A finding's id is the SHA-1 of its kind plus its **key**: the target (for example `override:in_game/common/building_types/some_building`) and, for a line finding, the key path, the key, and vanilla's old value, vanilla's new value and yours. Line numbers, detail text and tracker hashes are never part of it. The summary shows the first 8 characters, which `--dismiss` accepts (an ambiguous prefix is an error).

Because the id is content, a dismissal applies only while the facts are the same. When vanilla changes the value again or you change yours, the id changes and the finding comes back. Duplicate definitions cannot be dismissed.

### Open findings and carry-forward

Default runs (no `--old`, `--new`, `--full`, `--block` or `--category`) replace the `open` map with this run's actionable, undismissed findings, keeping the `since` and `base` of any finding that was already open. Findings no longer produced are closed. On the next run, the base of an open finding feeds baseline selection (section 9) and the localization audit (section 7), so drift keeps being reported until it is fixed or dismissed, even after the window moves past its patch. The summary lists these under **Still open from earlier patches**.

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

Baseline detection reads parsed indexes at every tracked snapshot, not just two, so each is cached on disk under `<vanilla-tracker>/cache/`, keyed by the commit's full hash: block indexes, GUI indexes, vocabularies, and the duplicate audit's definition index.

- A commit's content is immutable, so a cache entry for a given hash is never stale.
- A version number in the file name (`gui-v1-...`) lets a parser change retire old entries.
- Entries for commits no longer in the tracker are pruned at the start of each run, one validated file at a time.

The first run after a new snapshot pays to read that snapshot once; every run after is served from cache.

Within a single run, the audits also share their work. The mod's module-root folders are listed once, each mod file is read once, each commit hash is resolved once, and each distinct vanilla text is normalized and parsed once, no matter how many audits or snapshots use it. The localization audit is not cached on disk. It reads only the vanilla `.yml` files in the languages the mod defines, in tree order so the first file defining a key still wins, and parses each distinct file once, so the old and new versions share every file vanilla left unchanged. Nothing is shared between runs, so edits to the mod between runs are always seen.

---

## 13. Deletion safety

pdx-audit deletes files in exactly three situations: a stale snapshot index, cache entries for commits that left the tracker, and orphaned records on request. All three go through one function, `pdxaudit.safety.remove_file`, which removes a single file only when:

- its name fully matches the expected pattern (for a record, 40 hex characters plus `.json`);
- after resolving the path, it sits directly inside the expected folder, and that folder is not a filesystem root;
- it is a regular file, not a symlink or a directory.

Anything else raises and leaves the target alone, so a malformed or hallucinated path cannot remove a directory tree. pdx-audit never removes folders, even empty ones.

`tests/test_no_recursive_removal.py` enforces this. It parses every source file with Python's `ast` module and fails on `shutil.rmtree`, `os.removedirs`, `os.rmdir`, `Path.rmdir`, `tempfile.TemporaryDirectory`, `os.system`, subprocess calls to `rm`-style commands, and any `unlink`/`os.remove` outside the helper. It is a tripwire for ordinary code, not a sandbox.

---

## 14. Baseline selection reference

| Invocation | Baseline for REPLACE blocks and GUI copies | Records updated |
|------------|--------------------------------------------|-----------------|
| `pdx-audit` | recorded, else detected by adoption, else the last patch | yes |
| `pdx-audit --full` | the oldest snapshot, fixed for everything | dismissals only |
| `pdx-audit --old X --new Y` | version X, fixed | dismissals only |
| `pdx-audit --block B` / `--category C` | as the default | dismissals only |

`new` is the newest snapshot unless `--new` overrides it. Dismissals always apply; open findings are only rewritten by default runs, so a filtered or fixed-window run never closes findings it did not look at.

---

*This document describes internal behavior and may lag the code. When in doubt, the code is the source of truth.*
