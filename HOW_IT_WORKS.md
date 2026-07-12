# How pdx-audit Works

A technical walkthrough of what the tool does behind the scenes. The README covers usage; this document covers mechanism. It assumes you have read the README and want to understand *why* the audit reports what it reports.

## Contents

1. [The problem being solved](#1-the-problem-being-solved)
2. [The vanilla tracker](#2-the-vanilla-tracker)
3. [The override surface](#3-the-override-surface)
4. [The containment check (the core idea)](#4-the-containment-check-the-core-idea)
5. [Override audit](#5-override-audit)
6. [Dependency audit](#6-dependency-audit)
7. [Localization audit](#7-localization-audit)
8. [GUI audit](#8-gui-audit)
9. [Fork points](#9-fork-points)
10. [Caching](#10-caching)
11. [The stamp command](#11-the-stamp-command)
12. [Baseline selection reference](#12-baseline-selection-reference)

---

## 1. The problem being solved

A mod overrides pieces of the base game. When the game patches, the base game moves and the mod's copies do not. Three things can go wrong:

- A block you replaced wholesale gains new lines in vanilla that your replacement now silently drops.
- A block you injected into moves or disappears, so your injection lands nowhere.
- A modifier or trigger you reference gets renamed or removed, so your reference is dead.

None of these produce an error at load time. The mod just quietly does the wrong thing. pdx-audit exists to make that drift visible before it ships.

The whole tool rests on one comparison: **your override, the game's version of the same thing before the patch, and the game's version after the patch.** Everything else is machinery to make that three-way comparison accurate and cheap.

---

## 2. The vanilla tracker

The audit needs the game's files at more than one point in time. It keeps them in a bare git repository called the **vanilla tracker**, where each commit is one game version:

```
cef54d2  1.3.10 Pavia      <- newest
771f2ce  1.3.8 Pavia
a3d68da  1.3.6-beta Pavia
0b8980e  1.3.4 Pavia
...
741b7ea  1.2.0 Echinades   <- oldest
```

Each commit holds the `.txt`, `.yml`, and `.gui` files from that version's install. `--snapshot <version>` reads the live install and adds a commit. Because git orders commits, "the previous patch" is just "the commit before HEAD," and any two versions can be diffed.

Two terms used throughout:

- **old / new**: the two versions being compared in a given run. By default `new` is the newest snapshot and `old` is the one before it (the last patch). Flags can move both.
- **snapshot**: one committed game version in the tracker.

---

## 3. The override surface

The tool scans the mod for the places it overrides vanilla. In script files that means directives like:

```
REPLACE:some_block = { ... }       # replace vanilla's block entirely
INJECT:some_block = { ... }        # add to / modify vanilla's block
TRY_INJECT:maybe_block = { ... }   # inject only if the target exists
```

Each directive names a target block that also exists in vanilla. The audit's job is to look up that target in the old and new snapshots and see what changed.

In GUI files the override is implicit: if the mod defines a `template` or `type` with the same name as a vanilla one, or ships a `.gui` file at the same path as a vanilla file, it overrides it. There is no keyword; sameness of name or path *is* the override. This is why the GUI audit is a separate pass with its own logic (section 8).

---

## 4. The containment check (the core idea)

The naive way to flag drift is: "did vanilla's version change between old and new?" That is wrong, because it fires whenever vanilla changed anything, even something your override already accounts for. The right question is narrower:

> Of the things vanilla changed, which ones is *your copy* missing?

This is the **containment check**. Given your override text and vanilla's old and new text, it works in three steps:

1. Diff vanilla old against vanilla new. This yields the lines vanilla **added** and the lines vanilla **removed**.
2. For each **added** line, check whether it already appears in your override. If it does not, your copy is behind: record it as **missing**.
3. For each **removed** line, check whether your override still carries it (and vanilla no longer has it anywhere). If so, your copy holds a line the game deleted: record it as **kept**.

If there are no missing and no kept lines, your override already reflects the change and there is nothing to do. Otherwise you have concrete lines to look at.

### Worked example

Vanilla's block gains a line between 1.3.8 and 1.3.10:

```
# vanilla 1.3.8            # vanilla 1.3.10
cost = 100                 cost = 100
                           upkeep = 5      <- added
```

Your REPLACE copy:

```
# your override
cost = 100
upkeep = 5
maintenance = local
```

The diff says vanilla added `upkeep = 5`. The check looks for `upkeep = 5` in your override, finds it, and reports **nothing to do**. Your extra `maintenance = local` is ignored, because the check only asks about lines *vanilla* moved, not lines you added.

### Why this is robust to how far back you look

Both steps are gated on *your* text, not on the old snapshot. A line vanilla added long ago and you already copied in will always be found present, no matter how old the `old` snapshot is. This is what lets the script audit compare against a distant baseline without drowning you in changes you already have. (The GUI audit's file-level path cannot lean on this, for reasons in section 8, which is where fork points come in.)

### The one caveat

The check is line-*set* based: a line counts as present if it appears *anywhere* in your override, not necessarily in the same block. For a small block that is almost always fine. For a whole file it is looser, so the GUI audit does not use containment for whole-file replacements.

---

## 5. Override audit

This is the default run. For each `INJECT`/`REPLACE`/`TRY_*` directive:

1. Look up the target block in the old snapshot and the new snapshot.
2. Branch on what exists where:

| old | new | meaning | reported as |
|-----|-----|---------|-------------|
| yes | yes, same text | vanilla left it alone | unchanged |
| yes | yes, different | vanilla changed it | run containment (below) |
| yes | no | vanilla removed the target | **orphaned override** |
| no  | no | target never existed in this window | not found (see below) |
| no  | yes | vanilla just added a same-named block | new collision |

3. For a changed target, run the containment check. A REPLACE that is missing vanilla's new lines is **stale** and is the highest-priority finding. One that already contains them is **reconciled** and reported only as a count.

**TRY_ directives** deserve a note. `TRY_INJECT`/`TRY_REPLACE` mean "do this only if the target exists." So:

- If the target is absent from *both* snapshots, the directive was written for something that may not be present (another mod, a conditional vanilla block). That is listed as expected, not an error.
- If the target was present in the old snapshot and vanilla *removed* it, that is the "orphaned override" row above, and it is flagged like any other removal. A `TRY_` prefix does not hide a target that actually disappeared.

---

## 6. Dependency audit

Run with `--deps`. This one does not look at blocks; it looks at identifiers.

The mod assigns many tokens: `modifier = some_modifier`, `trigger = some_trigger`, and so on. If vanilla used a token in the old snapshot but no longer defines it in the new one, the token was probably renamed or removed, and the mod's reference is now dead.

The audit builds a vocabulary of every `token =` identifier vanilla defines at each snapshot, then flags identifiers the mod uses that vanilla dropped between old and new. For each dropped token it offers rename candidates by ranking the new snapshot's vocabulary by shared prefix length. This is the audit that would have caught the 1.3 batch of `_cost` to `_efficiency` modifier renames before they showed up as runtime errors.

Findings are suspects. A token can legitimately disappear from vanilla's vocabulary while still being a valid engine builtin; confirm with `pdx-syntax`.

---

## 7. Localization audit

Run with `--loc`. Localization lives in `.yml` files as `KEY: "value"` lines under a language header (`l_english:`, `l_french:`, ...). A mod overrides a vanilla string by redefining the same key in any of its own `.yml` files; load order decides the winner. There is no same-path or same-file requirement, so the audit cannot key on filenames.

Two facts shape it:

- **The unit is `(language, key)`, not `key`.** The same key exists once per language with a different value (`GREETING` is "Hello" in `l_english` and "Bonjour" in `l_french`). Matching on the bare key would compare English against French. So the language, read from the file's header, is part of the identity.
- **Matching is by name, never by file.** Vanilla renames loc files and moves keys between them constantly, so file-level matching would be useless. The audit builds a global `(language, key) -> value` view and ignores which file a key lives in.

The steps:

1. Parse every mod `.yml` to get the set of `(language, key)` pairs the mod defines. That set is your override surface.
2. For the old and new snapshot, scan vanilla's `.yml` and record the value of each of those keys (only those, so the working set stays small).
3. For each key, compare:

| old | new | meaning | reported as |
|-----|-----|---------|-------------|
| present, present, same | vanilla left the string alone | unchanged |
| present, present, different | vanilla reworded or corrected the string | **changed** |
| present, absent | vanilla removed the key | **orphaned override** |
| absent, present | vanilla just added a key you also define | new collision |
| absent, absent | the key is yours, not an override | mod-only |

The actionable one is **changed**: vanilla altered a string and your override still shows your own text, so whatever vanilla fixed or reworded is suppressed until you decide whether to update.

### Worked example

Your mod defines, in `english`, `BUILDING_FARM_DESC: "A farm."` Vanilla changes its own value from "A farm." to "A farm. Produces grain." between the two snapshots. The audit reports `BUILDING_FARM_DESC (english)` as changed, showing your value, vanilla's old value, and vanilla's new value, so you can see exactly what vanilla added and choose whether to fold it in. A key you invented that vanilla has never had (say `SUL_MY_FEATURE`) is counted as mod-only and never flagged.

### A note on scope

Most keys a large mod defines are its own new strings, not overrides. In one real run a mod defined ~4800 keys but only 67 of them matched a vanilla key; those 67 are the real override surface and the rest are mod-only. The audit only scans vanilla for the languages your mod actually ships, so an English-only mod is quick; a mod that ships all fifteen languages pays to read all fifteen.

---

## 8. GUI audit

Run with `--gui`. GUI overrides are implicit (section 3), so this pass has two distinct jobs.

### Path A: shadowed definitions

The mod defines a `template` or `type` whose name also exists in vanilla. First-loaded-name wins in the engine, so the mod's definition shadows vanilla's. The audit finds these by name, then runs the same old/new comparison and containment check as the script audit. A shadowed definition vanilla changed and your copy is missing is stale; one your copy already carries is reconciled.

### Path B: same-path file replacements

The mod ships a `.gui` file at the same path as a vanilla file, replacing it whole. Here containment does not help. Your file is not a pristine copy; you rebuilt parts of it. So "is your file missing a line vanilla added?" is true for a great many lines *by design*, and containment would flag nearly everything.

For a whole replaced file the only sensible question is textual: **did vanilla's version of this file change between the baseline and now?** If yes, your replacement may be suppressing something worth a look. That makes the choice of baseline the whole ballgame, which is section 9.

### The baseline problem, concretely

Suppose you copied `map_markers.gui` from version 1.2.2 and edited it. The tracker goes back to 1.2.0. If the audit compares vanilla's 1.2.0 version against the newest version, it reports *every* change vanilla made to that file across its entire history, including everything between 1.2.0 and 1.2.2 that you already have because you copied from 1.2.2. That is a flood of findings with nothing to fix.

The fix is not a cleverer diff. It is to compare against the version you actually started from.

---

## 9. Fork points

A **fork point** is the game version a given override was copied from: the version your file most closely resembles before your own edits. Measuring drift from the fork point forward reports only what vanilla changed *after* you branched, which is exactly the actionable set.

### How the fork point is detected

For each mod file (and each shadowed definition), the audit compares your copy against every tracked snapshot and scores the difference as the number of changed lines (added plus removed under a normalized line diff). The snapshot with the smallest difference is taken as the fork point.

Using the real detection on this mod's files:

```
economy_lateralview.gui  -> 1.3.10       (you started from current; nothing to report)
hud_topbar.gui           -> 1.3.10       (same)
location_window.gui      -> 1.3.2-beta   (older start; vanilla changed it since -> flagged)
map_markers.gui          -> 1.2.2        (older start -> flagged)
map_markers_city.gui     -> 1.2.5        (older start -> flagged)
```

The first two resolve to the newest version, so there is no "after" to report and they drop out. The other three resolve to older versions, so vanilla's later changes to them surface. This is why the default GUI audit reports three replaced files where a fixed oldest-baseline run reports five: the two extra are files you already have up to date.

The report always prints the detected fork point per file, so the inference is visible, never hidden. A fork point several patches back is a hint that your copy has diverged a lot and may be worth a manual look.

### Pinning a fork point

Detection is a heuristic. If it guesses wrong, override it with a comment at the very top of the GUI file:

```
# pdx-audit fork-point: 1.3.8
```

When present, the audit uses that version as the baseline for the file (and for the definitions in it) instead of guessing. The token can be a version tag or a commit hash. If it matches no tracked snapshot, the audit warns and falls back to auto-detection rather than failing.

### Stale pins

A pin is authoritative, which means a wrong pin is obeyed. If you pin a file to 1.3.10, later update the file to be compatible with 1.5, but never update the comment, the audit will keep measuring from 1.3.10 and re-report the 1.3.10 to 1.5 changes you already made.

To catch this, the audit still runs detection even on pinned files, purely to compare. If a file's contents look clearly closer to a different version than the one it pins, it prints a single-line warning:

```
Warning: map_markers.gui pins fork-point 1.3.10, but its contents look
  closer to 1.2.2; the pin may be stale (--stamp-fork-points --refresh updates it).
```

The pin is still honored for the audit itself; the warning only tells you it may be worth updating. It fires only when the gap is real (the file is measurably closer to another version), so ordinary edit noise does not trip it.

This is the default behavior, not an opt-in. `--full`, `--old`, or `--new` turn it off and use a fixed window instead (section 12).

---

## 10. Caching

Fork-point detection reads the parsed GUI index at *every* tracked snapshot, not just two. Doing that from scratch on every run would make the common case slow to serve the wide case, so the parsed index for each snapshot is cached on disk under `<vanilla-tracker>/cache/`, keyed by the commit's full hash.

Two properties make this safe:

- A commit's content is immutable, so a cache entry for a given hash is never stale. There is no invalidation logic because none is needed.
- A version number in the cache key (`gui-vN-...`) lets a change to the parser retire old entries automatically.

The first GUI audit after a new snapshot pays to read that snapshot once; every run after is served from cache. The dependency audit's vocabulary is cached the same way.

---

## 11. The stamp command

`--stamp-fork-points` writes the detected fork point into each replaced GUI file as a `# pdx-audit fork-point:` comment, so the baseline is locked in and visible in the file itself rather than re-inferred each run.

Because it modifies the mod's own source files, it is deliberately cautious:

1. It detects the fork point for every same-path replacement file.
2. It skips files that already carry a correct pin and reports them as left alone. Files whose pin looks stale are listed, with a note to re-run with `--refresh`.
3. It prints the full list of files it would change and the exact comment it would add or the exact `old -> new` change it would make.
4. It states plainly that this writes to your source files.
5. It waits for an interactive yes. If input is not a real terminal (for example a piped or scripted run), it refuses to write at all.

On confirmation it inserts the comment as the first line of each unpinned file, preserving a leading byte-order mark if the file has one. Nothing is written unless you type yes at the prompt.

### Refreshing stale pins

`--stamp-fork-points --refresh` extends the command to also rewrite pins the audit flags as stale (section 9). A refresh rewrites the existing comment in place, changing only the version and leaving the rest of the file, including its byte-order mark, untouched. It follows the same confirmation flow: the plan shows each change as `old_version -> new_version`, and nothing is written without an interactive yes. Correct pins are never touched.

---

## 12. Baseline selection reference

The GUI audit's baseline depends on which flags are present:

| Invocation | `old` baseline | When to use |
|------------|----------------|-------------|
| `pdx-audit --gui` | per-file fork point (detected or pinned) | normal patch check; the default |
| `pdx-audit --gui --full` | oldest snapshot, fixed for all files | from-scratch review of total divergence |
| `pdx-audit --gui --old X --new Y` | version X, fixed | comparing two specific versions |

`new` is the newest snapshot unless `--new` overrides it.

The script override audit and dependency audit are not fork-relative; they rely on the containment check (section 4), which is already accurate across a wide window, so their default is the plain one-patch-back window with `--full` available to widen it.

---

*This document describes internal behavior and may lag the code. When in doubt, the script is the source of truth.*
