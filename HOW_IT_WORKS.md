# How pdx-audit Works

This document gives what the audits compare, and why they report what they report. The README gives the usage.

## What the audits compare against

pdx-audit keeps the `.txt`, `.yml` and `.gui` files of the game, and the small text formats `.csv`, `.map`, `.shader`, `.fxh` and `.asset`, in a bare git repository, the tracker. Each commit is one game version:

```
23272f5  1.3.11 Pavia      <- newest
cef54d2  1.3.10 Pavia
741b7ea  1.2.0 Echinades   <- oldest
```

A run compares the mod against a window of these versions. `new` is the newest version and `old` is the oldest, and `--old` and `--new` move the window. A record stores the version tag (`1.3.10`) and never a commit hash, because each user builds a different tracker.

## The copy and its baseline

Four things are each a **copy**: a REPLACE block, a GUI template or type that the mod defines again, a `.gui` file at the path of a vanilla file, and each top-level definition of any other file at the path of a vanilla file. A copy is vanilla text from some game version, with your edits on top.

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

- **Override** (`--overrides`): each `REPLACE:` block against its own history, by the method above. An `INJECT:` adds children to a vanilla block, so the audit compares the top-level keys of that block between the old version and the new version. A key that the INJECT sets and vanilla also changed is stale, because the final value in the game is different. A target that vanilla removed is an orphaned override. So is a target that vanilla defines only in a file the mod replaces at the same path, when the mod's copy of that file does not define it: the copy loads instead of vanilla's file, and the target does not exist.
- **Dependency** (`--deps`): each name that your script uses against the vocabulary of vanilla at each version, and each GUI name that your `.gui` files use against vanilla's `.gui` files. The audit reports a name that vanilla used at an earlier version and no longer uses, with the patch that removed it. It leaves out the names that the mod defines itself.

  A finding can name a **rename candidate**. The audit does not guess it from the spelling. It compares each vanilla file that used the name with the same file at the next version, line by line. A line that used the name and that vanilla replaced is one site. The candidate is the name of the same kind that the replacement lines use at most sites, and it must hold at least half of them. For example, vanilla 1.4 uses `header_action_button_left_uber` in place of `header_action_button_left` at 30 of 34 sites. A candidate is for review only. No tool applies it.

  The audit also reads the GUI names of your `.gui` files with the structural parser. A **type** is a widget key (`header_action_button_left = {`) or the parent of a type (`type my_button = header_action_button_left {`). A **template** is `using = name`, for a template or a local_template. A **block** is `blockoverride "name"`, for a `block "name"` of vanilla. The audit reports a type, a template or a block that vanilla's `.gui` files defined at an earlier version and do not define at the new version. It reads the `.gui` files of all modules, because `in_game` uses the font templates of `loading_screen` and the templates of `main_menu`.

  A **data-binding name** is a promote or a function in a `[...]` expression, such as `ImportExportMarker` in `[ImportExportMarker.GetLocation]`. The tracker holds no list of the names that the engine knows. So the audit reports a name that vanilla's `.gui` files used at an earlier version, and that no `.gui` file and no English localization file of vanilla uses at the new version. This finding is for review: the engine can still know a name that vanilla stopped using. The config key `engine_data` can name the pdx-syntax database. The audit then reads its `data_types` table: it leaves out a name that the engine still knows, and reports the others as broken. A string argument in single quotes, a format after `|`, and a game-concept link such as `[market|e]` are not data-binding names.
- **GUI** (`--gui`): a GUI override has no keyword. The same name or the same path *is* the override. The audit compares each copy against its history, and reports all the changes in one block as one finding.
- **Same-path file** (`--files`): a mod file at the path of a vanilla file replaces vanilla's whole file. This audit reads every such file that the GUI and localization audits do not: events, map data, setup files, plain definitions in `common/`, `.csv` files and shaders. A script file is split into its top-level definitions, and each definition is a copy, compared with vanilla's versions of the same definition by the method above, with its own baseline. Top-level statements that are not blocks, such as `namespace`, count as one more definition. A definition that vanilla added after the version the copy's definitions match is missing from the game, unless another mod file in the same folder keeps it. A definition that vanilla deleted and the copy keeps is reported for review. A file that is not script is compared line by line. A definition or file too large to compare statement by statement (a generated locator file holds one block of 20,000 entries) gets one finding that says how many lines vanilla changed after the version the copy matches. A file that the tracker holds no versions of, such as an image, is listed as not audited.
- **Localization** (`--loc`): the unit is `(language, key)` and never a file name, because vanilla moves keys between files. The audit reports a key whose vanilla value changed or went away.
- **Duplicate** (`--dupes`): one source of truth for each definition. The audit reports a name that the mod defines or overrides in more than one place, such as two INJECTs, or an INJECT and a REPLACE. It reads the `common/` folders and the event ids in the `events` folders. It leaves out the types that the engine merges across files, such as on_action, and finds those types in vanilla itself.

## Severity

The summary uses three levels:

- **broken**: the override cannot operate as written, or a definition has more than one source.
- **stale**: the override operates, but hides content that vanilla added.
- **review**: a change of vanilla to examine.

## Findings and dismissals

A finding gets its id from its content: the target, the keys of the blocks around the change, and the text of both sides with the layout removed. Line numbers are not part of it. Because of this, an id survives an unrelated edit, and it is the same for each user on the same game version.

A dismissal applies while that content is the same. When vanilla changes its statement again, or you change yours, the id changes and the finding comes back. You cannot dismiss a duplicate in the mod, because it has one remedy: keep one definition.

pdx-audit keeps the record in the data folder of the user, with one file for each git commit of the mod. It never writes in the mod, and it never removes a folder. A branch sees the decisions up to its start point, and its own decisions.

The cache of what pdx-audit reads from each vanilla version is in the same data folder, in `tracker-cache`, with one folder for each tracker. pdx-audit only reads the tracker, and writes nothing into it or next to it.

A finding stays in the report until you correct it or dismiss it, also after later commits move the window past its patch.

## The intent store

A **deviation** is one difference between a copy and the current text of vanilla: one change of the table above, of any kind, your own edits included, or one child that an INJECT sets. The store explains deviations with rules and entries.

Each deviation has an **address**. The address names the copy by vanilla identity: the content folder and the block name for script (`in_game/common/laws/law_a`), the template or type for GUI, or the path for a same-path GUI file. It never names the mod file, so a block that you move to another file keeps its address. Inside the copy, the address names each node by the fields the alignment pairs siblings by: the key, the `name`, the selector (`limit`, `trigger` or `id`, by a hash of its text), a distinctive quoted value, and a position among siblings that share all of these. So two `if` blocks with different limits have different addresses.

An entry records the hash of its node on both sides when you confirm it. On each run the entry is **recorded** when both hashes are the same, **stale** when vanilla or your node changed, and **lost** when the node is gone. Only a recorded entry explains a deviation. An entry wins over a rule. Two rules with different dispositions on one deviation explain nothing, and the conflict is reported.

The **baseline** records the deviations that nothing explains at one moment. The gate (`intent check`) exempts them until they change, or until vanilla changes them after the baseline. A new or changed deviation needs a rule or an entry. The deviations of each copy go into a cache in the tracker's cache folder (`devs-v1-<commit>.json`), keyed by a hash of the copy's text and of vanilla's history. A check of some files reads only their copies.

A file that a tool generates (an `AUTO-GENERATED` header, or an output of a tool in `pdx-maint.toml`) is never proposed or merged. Its tool reads vanilla again: `pdx-maint run <id>` regenerates it.

## The merge

The merge reads three texts of each copy: vanilla at the copy's baseline (not later than `--old`), your copy, and vanilla at `--new`. It parses them and pairs the nodes of each level. Identical nodes pair first, in order. Between them, nodes of one key pair in order by how many statements they share, and the pairing of the base with your copy also counts how close your node is to vanilla's new node. So a block that vanilla inserted before a changed sibling of the same key does not take that sibling's place. A named block, a block with a selector, an identical node and a key that only one node holds pair also when they moved.

Each base node is decided alone: vanilla's change is taken where your node is the base's, your node stays where vanilla did not change it, and a block that both changed is merged inside. Where both changed one statement, the intent store decides, or the node is an open decision. A node that only vanilla has goes after the counterpart of its nearest earlier sibling, with the comment lines above it. The merge writes into your text at the offsets of the nodes, so your layout, your order and your comments stay.

Vanilla's comments go with the nodes they describe. A changed or inserted vanilla node brings the comment lines above it and the comment after it on its line, unless you changed that comment. A comment that vanilla writes in the place of a removed statement takes the statement's place.

A node of your copy can be vanilla's text from a version later than the base, or a vanilla block that you moved, as it is, into a block of your own. The merge reads vanilla's versions between the base and `--new` to find such nodes, and vanilla's later change to them is a decision. A new vanilla block that holds a block you moved is open, so the moved block is never written two times.

In a same-path script file, the merge also adds the top-level definitions that vanilla added after `--old`. Each one passes through the intent store: a rule matches it by the change kind `vanilla_added` and the path pattern `[""]`. Text that vanilla did not hold at `--old` has an empty base. A key that both sides added in different forms is a `both_added` decision.

The plan holds the merged text after one pdx-format pass, when your file is in pdx-format layout already. Each decision holds the base, your text and vanilla's text, and the base version.

## The version window

| Invocation | Versions for copies | Window for the other audits |
|---|---|---|
| `pdx-audit` | each commit up to the newest | the last patch |
| `pdx-audit --full` | each commit up to the newest | the oldest commit to the newest |
| `pdx-audit --old X --new Y` | X through Y | X to Y |

A default run writes the open findings. A run with a filter or a fixed window applies the dismissals, but never closes a finding that it did not examine.

---

*This document gives internal behaviour, and it can be older than the code. The code is the source of truth.*
