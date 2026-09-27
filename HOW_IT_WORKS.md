# How pdx-audit Works

This document gives what the audits compare, and why they report what they report. The README gives the usage.

## What the audits compare against

pdx-audit keeps the `.txt`, `.yml` and `.gui` files of the game in a bare git repository, the tracker. Each commit is one game version:

```
23272f5  1.3.11 Pavia      <- newest
cef54d2  1.3.10 Pavia
741b7ea  1.2.0 Echinades   <- oldest
```

A run compares the mod against a window of these versions. `new` is the newest version and `old` is the oldest, and `--old` and `--new` move the window. A record stores the version tag (`1.3.10`) and never a commit hash, because each user builds a different tracker.

## The copy and its baseline

Three things are each a **copy**: a REPLACE block, a GUI template or type that the mod defines again, and a `.gui` file at the path of a vanilla file. A copy is vanilla text from some game version, with your edits on top.

The audit must tell your edits from the changes of vanilla. It reads the copy at each version in the window and finds the version that the copy differs from least. That version is the **baseline**: the version that you took the copy from. If two versions are equally close, pdx-audit uses the older one.

## How a difference gets a cause

The audit parses your copy and the newest vanilla text into statements. It pairs them by the same statement, then by the same key, then by the same distinctive quoted value under a different key. Layout, comments and the spelling of numbers are never a difference. The audit then finds each difference in the history of vanilla at the same location, and the baseline tells it who made the difference:

| Difference | Meaning | Priority |
|---|---|---|
| vanilla changed | your value is a value that vanilla had here, and vanilla changed it | high or mid |
| vanilla added | vanilla added a statement, and your copy does not have it | high or mid |
| vanilla removed | your copy keeps a statement that vanilla deleted | high or mid |
| vanilla renamed | vanilla moved a block to another key, and your copy still sets the old one | high or mid |
| both changed | vanilla changed a statement that you also changed, after your baseline | mid |
| your edit | vanilla did not change the statement, or changed it at or before your baseline | not reported |

A change of vanilla is **high** if its block also contains an edit of yours, because the two compete. In all other conditions it is **mid**. A conflict is mid: your copy replaces the block of vanilla, so your value applies before and after the change, and the game operates as before.

For example, vanilla reads:

```
1.0:   cost = 100
1.1:   cost = 200   upkeep = 5
```

Your copy reads `cost = 100` and `custom = yes`. The baseline is 1.0. `cost` is an old value of vanilla, `upkeep` is an addition of vanilla, and `custom` is your edit. Your edit is in the same block, so both changes of vanilla are high.

Your copy can hold a vanilla statement as a comment, word for word (`#trade_income = 0.1`). That is your own deletion, and the audit does not report it. A statement that vanilla added at or before your baseline is also your own deletion, because you saw it when you made the copy.

A rename reads as one addition and one removal of the same text. The audit uses the history of vanilla to tell them apart. Vanilla held a block under the old key, and it holds a block under the new key. If the two read the same, they are one change. Only a block counts, because two short statements read alike too often. A history that starts after you made the copy cannot tell your edits from the earlier changes of vanilla. A difference that is older than the oldest commit reads as yours.

## What each audit compares

- **Override** (`--overrides`): each `REPLACE:` block against its own history, by the method above. An `INJECT:` adds children to a vanilla block, so the audit compares the top-level keys of that block between the old version and the new version. A key that the INJECT sets and vanilla also changed is stale, because the final value in the game is different. A target that vanilla removed is an orphaned override.
- **Dependency** (`--deps`): each name that your script uses against the vocabulary of vanilla at each version. The audit reports a name that vanilla used at an earlier version and no longer uses, with the patch that removed it. It does not guess renames.
- **GUI** (`--gui`): a GUI override has no keyword. The same name or the same path *is* the override. The audit compares each copy against its history, and reports all the changes in one block as one finding.
- **Localization** (`--loc`): the unit is `(language, key)` and never a file name, because vanilla moves keys between files. The audit reports a key whose vanilla value changed or went away.
- **Duplicate** (`--dupes`): one source of truth for each definition. The audit reports a name that the mod defines or overrides in more than one place, such as two INJECTs, or an INJECT and a REPLACE. It leaves out the types that the engine merges across files, such as on_action, and finds those types in vanilla itself.

## Severity

The summary uses three levels:

- **broken**: the override cannot operate as written, or a definition has more than one source.
- **stale**: the override operates, but hides content that vanilla added.
- **review**: a change of vanilla to examine.

## Findings and dismissals

A finding gets its id from its content: the target, the keys of the blocks around the change, and the text of both sides with the layout removed. Line numbers are not part of it. Because of this, an id survives an unrelated edit, and it is the same for each user on the same game version.

A dismissal applies while that content is the same. When vanilla changes its statement again, or you change yours, the id changes and the finding comes back. You cannot dismiss a duplicate in the mod, because it has one remedy: keep one definition.

pdx-audit keeps the record in the data folder of the user, with one file for each git commit of the mod. It never writes in the mod, and it never removes a folder. A branch sees the decisions up to its start point, and its own decisions.

A finding stays in the report until you correct it or dismiss it, also after later commits move the window past its patch.

## The version window

| Invocation | Versions for copies | Window for the other audits |
|---|---|---|
| `pdx-audit` | each commit up to the newest | the last patch |
| `pdx-audit --full` | each commit up to the newest | the oldest commit to the newest |
| `pdx-audit --old X --new Y` | X through Y | X to Y |

A default run writes the open findings. A run with a filter or a fixed window applies the dismissals, but never closes a finding that it did not examine.

---

*This document gives internal behaviour, and it can be older than the code. The code is the source of truth.*
