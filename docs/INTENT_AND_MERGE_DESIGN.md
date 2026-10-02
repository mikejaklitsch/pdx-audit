# Intent Rules and Node Merge: Design

Status: approved on 2026-10-01 with the decisions of section 14, which replace the text above them where the two differ. Phase 0 (removed names in the dependency audit) is built: see `pdxaudit/gui_names.py`. Phases 2 to 4 are built in order.

## 1. The problem

A game patch changes vanilla. The mod holds copies of vanilla text with its own edits on top. A port must keep each deliberate edit and take each vanilla change. Today the tools find the differences (`pdx-audit`), but no tool knows why the mod differs. The 1.4 port showed these faults:

- A mod that keeps an old vanilla value on purpose looks like a mod that did not change. A merge then applies the vanilla change, and the mod loses its decision.
- A line merge (port_merge v1) made wide conflicts. A key-grouped merge (v2) moved repeated keys, fused adjacent edits into one conflict, lost a vanilla change, and added one statement two times.
- A per-item keep list (`tools/port_merge_keep.txt`) does not scale. The user rule is: "Overarching system based rules are the only reliable method."
- No merge covered INJECT targets (176), whole-file copies (122), GUI copies, or renames.
- Keys that follow the mod file path broke when vanilla moved a block to another file, and when the mod moved a block into an `fe_` REPLACE file.

This design adds two things to pdx-audit:

1. An **intent store**: system rules and one-off entries that record why the mod differs from vanilla, held per user, never in the mod.
2. A **merge mode**: a node-level three-way merge that applies the store, and lists the decisions that are still open.

A lint gate (pdx-lint) keeps the store current: a new deviation needs a rule or an entry.

The tools never write a reason that the user did not give. A proposer drafts candidates, and the user confirms them.

## 2. Terms

| Term | Meaning |
|---|---|
| copy | A REPLACE block, an INJECT target, a definition in a same-path file, a shadowed GUI template or type, or a same-path GUI file. The audits already find each copy. |
| node | A statement or a block of parsed text: a `diff3.Node`. |
| deviation | A node where the copy differs from vanilla at the new version: each `diff3.Change`, of every kind (mod and vanilla kinds). |
| identity | The name of a copy that does not depend on a file path (section 5). |
| address | The identity of a copy plus the path of a node inside it (section 5). |
| rule | A system rule: one reason that explains many deviations, selected by a matcher. |
| entry | A one-off record for one deviation, at one address. |
| disposition | What a merge does with a deviation that a rule or an entry covers. |

## 3. The intent store

### 3.1 Location

The store is part of the per-user record that `store.py` writes now:

```
<data>/<mod id>/commits/<commit>.json
    { "dismissed": {...}, "open": {...}, "intent": {...} }
```

`ledger.empty_state()` gets a third key, `intent`. A record has branch semantics today: a branch sees the decisions up to its start point, and its own decisions. Rules and entries get the same semantics. pdx-audit never writes them into the mod.

### 3.2 Schema

```json
"intent": {
  "version": 1,
  "rules":   { "<rule id>":  { ...rule... } },
  "entries": { "<entry id>": { ...entry... } },
  "baseline": { "set_on": "2026-10-02", "mod_commit": "<sha>", "vanilla": "1.4.0-beta",
                "deviations": ["<deviation id>", ...] }
}
```

A **rule**:

```json
{
  "id": "provisions.food_cancels",
  "system": "provisions",
  "reason": "Provisions are the only food. Goods output modifiers replace the vanilla food modifiers.",
  "source": {"kind": "note", "ref": "176"},
  "disposition": "keep_mod",
  "match": { ...matcher... },
  "check": {"lint": "food_modifiers"},
  "rename": null,
  "created": "2026-10-02",
  "confirmed_by": "user"
}
```

- `id`: the system id, a dot, and a short name. The user writes it.
- `system`: a system id from `[system.<id>]` in `pdx-maint.toml`. pdx-audit reads the registry (read only, with `tomllib`) and refuses an unknown id. A mod without a registry uses free text.
- `reason`: one sentence. Required. The tools never fill it.
- `source.kind`: `note` (a pdx-maint note id), `commit` (a mod commit), or `user`.
- `check`: optional. The name of a mod lint check that enforces the same rule (section 4.3). pdx-audit does not run it. The proposer and the merge report name it.
- `rename`: optional, for `banned` (section 9).

An **entry**:

```json
{
  "id": "e-3fa2c1d0",
  "address": { ...address, section 5... },
  "scope": "subtree",
  "system": "complacency",
  "reason": "SUL penalties. Vanilla 1.4 flat penalties replace complacency, which SUL keeps.",
  "source": {"kind": "note", "ref": "176"},
  "disposition": "keep_mod",
  "seen": {"vanilla": "1.4.0-beta", "vanilla_sig": "<sha1>", "mod_sig": "<sha1>"},
  "created": "2026-10-02",
  "confirmed_by": "user"
}
```

- `scope`: `node` (the addressed node only) or `subtree` (the node and everything under it). A subtree entry is the default: `decline_of_empire.modifier` keeps the whole block with one entry.
- `seen`: the normalized text hash of the vanilla node and of the mod node when the user confirmed the entry. These give the state (section 3.4).

### 3.3 Dispositions

| Disposition | Merge | Lint |
|---|---|---|
| `keep_mod` | Keep the mod node. Also when the mod node equals the old vanilla node: this is the "deliberately kept old value" case. | Attributed. |
| `take_vanilla` | Take vanilla's node at the new version. | Attributed. |
| `merge` | Recurse into the children and merge them one by one. For a statement, the conflict stays open. | Attributed. |
| `banned` | The matched name must not occur in the mod. With `rename`, the merge writes the new name. Without it, the merge lists an open decision. | Each use is a finding. |

A rule and an entry can both match one deviation. The entry wins, because it is more specific. Two rules with different dispositions on one deviation are a store error: `intent check` reports it, and the merge treats the deviation as open.

### 3.4 States

pdx-audit computes the state on each run. It never stores it.

| State | Applies to | Meaning |
|---|---|---|
| `recorded` | entry | The address resolves, and both hashes equal `seen`. |
| `stale` | entry | The address resolves, but vanilla or the mod changed the node since `seen`. The report says which side. |
| `lost` | entry | The address does not resolve at the new version (vanilla or the mod deleted the node). |
| `attributed` | deviation | A rule or a `recorded` entry covers it. |
| `unattributed` | deviation | Nothing covers it. |

A stale entry still applies in a dry run, with a mark, but `--apply` refuses to apply it. The user confirms it again with `intent confirm <id>`. The tools never infer a reason, and never move an entry to `recorded` on their own.

## 4. System rules

### 4.1 Matcher

A matcher selects deviations across many files. Each field is optional. All fields that are present must match. A list in a field means "any of".

| Field | Matches | Example |
|---|---|---|
| `audit` | the copy type: `replace`, `inject`, `file`, `gui_def`, `gui_file` | `["replace", "inject", "file"]` |
| `content` | the content folder of the identity (`common/laws`) | `["common/laws", "common/gods"]` |
| `file` | a glob over the vanilla path or the mod path | `["in_game/gui/map_markers*.gui"]` |
| `block` | a glob or `re:` pattern over the block name | `["decline_of_*"]` |
| `path` | a pattern over the node path; `*` is one segment, `**` is any number | `["**.modifier.*"]` |
| `vanilla_key`, `mod_key` | the key of the vanilla node or of the mod node | `["re:^(local_\|global_)?monthly_food_modifier$"]` |
| `vanilla_value`, `mod_value` | the value of the node | `["re:^fe_"]` |
| `name` | a name anywhere in the node: a widget key, a `using`, a value | `["header_action_button_left"]` |
| `comment` | a comment on the node line or the line above | `["re:\\[FU\\]"]` |
| `change` | the `diff3.Change` kind | `["vanilla_changed", "both_changed"]` |
| `vanilla_absent` | true: vanilla has no node at this place | `true` |
| `transform` | a mod transform explains the deviation (section 4.2) | `"climate"` |

Patterns compare after normalization: the keyword case of `AND OR NOT NOR NAND` is upper case on both sides (the set in `pdx_format.constants.KEYWORDS_TO_UPPER`), and a BOM is removed. pdx-audit does not depend on pdx-format. The set moves to `pdx_utilities.constants`, and both tools import it there.

### 4.2 Reuse of the composer transforms

The composer (`tools/generate_vanilla_overrides.py`) already holds system rules as code: `rank_rewrite`, `climate_rewrite`, `raw_goods_rewrite`, `river_rewrite`, `goods_demand_rewrite`, `cheap_first`, the complacency decay and the rank flags. The mod lint check `vanilla_transforms` already requires each hand-owned block to carry them. The intent store must not copy these rules into matchers.

A `transform` matcher asks the mod: "Does transform T, applied to vanilla's node, give the mod's node?" If yes, the transform explains the deviation. The mod declares one adapter in the pdx-audit config (per user, not in the mod):

```json
"transforms": { "module": "tools/intent_transforms.py" }
```

The adapter module lives in the mod and exposes `TRANSFORMS = {"climate": fn, "rank": fn, ...}`, each `fn(text, content_dir) -> text`. It wraps the existing `process_text` functions. pdx-audit imports it only when a rule names a transform. The adapter is mod code, so the mod session writes it, not pdx-audit.

### 4.3 Relation to the mod lint checks

Some mod lint checks already enforce a rule: `food_modifiers`, `vanilla_rank_flags`, `vanilla_transforms`, `override_targets.INTENTIONAL_DROPS`. A rule names its check in `check`. The rule explains the deviation to the merge and the audit. The check keeps enforcing it in the mod. Neither replaces the other.

### 4.4 The six example rules

These show that the matcher can express each case. The reasons and the dispositions are drafts from notes #176 and #178. The user must confirm each one (see the open questions).

```toml
[[rule]]
id = "provisions.food_cancels"
system = "provisions"
disposition = "keep_mod"
source = { kind = "note", ref = "176" }
check = { lint = "food_modifiers" }
match = { audit = ["replace", "inject", "file"],
          vanilla_key = ["re:^(local_|global_)?monthly_food_modifier$", "hostile_food_multiplier",
                         "re:^(local|global)_monthly_food$"] }

[[rule]]
id = "economy.fe_unique_pms"
system = "economy"
disposition = "keep_mod"
source = { kind = "note", ref = "176" }
match = { content = ["common/building_types"], path = ["*.unique_production_methods", "*.unique_production_methods.**"] }

[[rule]]
id = "ranks.specialized_rank_flags"
system = "ranks"
disposition = "keep_mod"
check = { lint = "vanilla_rank_flags" }
match = { content = ["common/building_types"], change = ["mod_added"], transform = "rank_flags" }

[[rule]]
id = "climate.classes"
system = "climate"
disposition = "keep_mod"
check = { lint = "vanilla_transforms" }
match = { transform = "climate" }

[[rule]]
id = "integration.fu_marker_rate_kept"
system = "<open question>"
disposition = "keep_mod"
source = { kind = "note", ref = "178" }
match = { audit = ["gui_file", "gui_def"], file = ["in_game/gui/map_markers*.gui"],
          mod_key = ["max_update_rate"], comment = ["re:\\[FU\\]"], vanilla_absent = true }

[[rule]]
id = "integration.fu_marker_rate_vanilla"
system = "<open question>"
disposition = "take_vanilla"
source = { kind = "note", ref = "178" }
match = { audit = ["gui_file", "gui_def"], file = ["in_game/gui/map_markers*.gui"],
          mod_key = ["max_update_rate"], comment = ["re:\\[FU\\]"], change = ["vanilla_changed", "both_changed"] }

[[rule]]
id = "gui.no_header_action_button_left"
system = "<open question>"
disposition = "banned"
source = { kind = "note", ref = "178" }
match = { name = ["header_action_button_left"] }
rename = { from = "header_action_button_left", to = "header_action_button_left_uber", kind = "gui_type" }
```

The `[FU]` rules depend on a comment that marks the mod lines. If the mod lines carry no such comment, the matcher needs another field. This is an open question.

## 5. Identity and address

### 5.1 Identity of a copy

The identity follows vanilla, not the mod file path:

| Copy | Identity |
|---|---|
| REPLACE, INJECT, definition in a same-path file | `(content folder, block name)`, for example `("in_game/common/advances", "a_florentine_citizen_militia")`. The `REPLACE:`, `INJECT:`, `TRY_` and `_OR_CREATE` prefixes are removed. |
| shadowed GUI template or type | `(module, kind, name)`, as `gui.gui_def_target` gives now |
| same-path GUI file | `("guifile", path)`, and a top-level node path inside it |

The block index already keys vanilla by content folder and name, not by file. So a block that vanilla moved from the age 2 file to the age 3 file keeps its identity. A block that the mod moved from a copy into an `fe_` REPLACE file also keeps it. A move into another content folder breaks the identity. The merge reports it as a lost entry.

### 5.2 Address of a node

An address is the identity plus a list of segments. Each segment names one child by the same identities that `diff3.align` pairs siblings by, strongest first:

| Segment field | When | Example |
|---|---|---|
| `key` | always | `"if"` |
| `name` | the block sets `name = "..."` (GUI) | `"name=location_header"` |
| `sel` | script: the block has a `limit`, `trigger` or `id` child; the SHA-1 of its normalized text, first 8 hex digits | `"limit#3fa2c1d0"` |
| `val` | the node has a distinctive quoted value | `"val=\"[OnPause]\""` |
| `n` | two or more siblings share every field above; the 1-based position among them | `2` |
| `sig` | the SHA-1 of the node's own normalized text, first 8 hex digits | `"9b1e04aa"` |

A repeated `if` in a script block:

```
fe_monthly_complacency = {
    if = { limit = { has_x = yes } add = 1 }
    if = { limit = { has_y = yes } add = 2 }
    if = { limit = { has_y = yes } add = 3 }
}
```

| Node | Address |
|---|---|
| first `if` | `fe_monthly_complacency / if[sel=limit#a1..]` |
| second `if` | `fe_monthly_complacency / if[sel=limit#c7.., n=1]` |
| third `if` | `fe_monthly_complacency / if[sel=limit#c7.., n=2]` |
| `add` in the third | `fe_monthly_complacency / if[sel=limit#c7.., n=2] / add` |

The text form above is for people. The store holds the segment objects.

### 5.3 Resolution after a change

The address resolves by strongest field first. When a field no longer matches (vanilla changed the `limit`), pdx-audit follows the node through vanilla's history: `diff3._History` already aligns each tracked version with the version before it. The entry stores the version it saw (`seen.vanilla`). Resolution aligns forward from that version to the new one, one version at a time, and takes the counterpart. The resolved entry is then `stale`, never `recorded`, because the node changed.

Ordering matters for some repeated keys and not for others. The script dialect of diff3 already treats a block's children as a set, told apart by selector. `_settle` compares bare statements as a multiset. This design keeps that, and adds one rule for the merge: the output keeps the mod's order of statements (section 7.3). The keys where order matters (`if`, `else_if`, `else`, `select_trigger`, `change_variable`, `set_variable`) therefore stay in place. Keys where order does not matter (`mercenaries_per_location`, `gfx_tags`) also stay in place, and that is harmless. A bare value list merges as an ordered set: the mod's order first, then vanilla's new members in vanilla's order.

## 6. Proposer and seeds

### 6.1 Proposer

`pdx-audit intent propose` groups the unattributed deviations and drafts rule candidates. It writes a proposal file to `<data>/<mod id>/proposals/<timestamp>.toml`, never into the mod. Nothing becomes a rule or an entry until the user runs `intent accept`.

Evidence per deviation:

| Evidence | Source |
|---|---|
| system | the mod file of the copy against `[system.*].files` globs in `pdx-maint.toml` |
| commit | one `git blame -C -C --line-porcelain` call per mod file, with one `-L a,b` range for each run of deviation lines, run in a thread pool and kept for the run by the file's text. One `-C` follows lines that moved between files in one commit; `-C -C` adds copies from any file of the parent of the creating commit, which covers a block split out of another file. A third `-C` searches every file of every commit: on `/mnt/c` one such call took more than 1.5 minutes for one file, so the tools do not use it. |
| comment | the comment lines directly above the node or the block in the mod |
| note | `pdx-maint note search <block name>` and notes whose anchors name the file |
| check | a mod lint check whose source names the key (a plain text search of `tools/lint/*.py`) |

Grouping: deviations with the same (system, change kind, key or key pattern, content folder) form one candidate. The proposer drafts a matcher from the shared fields, and lists every deviation that the matcher selects, also the ones outside the group. So the user sees what a rule would cover before accepting it.

The proposer fills `system` and `source` from the evidence. It leaves `reason` empty. It shows the evidence text (commit subject, comment, note title) beside the empty field. `intent accept` refuses a candidate without a reason. The user confirms in batches: `intent accept <file> --only c3,c7,c9`.

### 6.2 Seeds

`pdx-audit intent seed` writes a proposal, not entries:

| Seed | Becomes |
|---|---|
| dismissals with a reason (19 in SUL now) | one-off entry candidates at the address of the finding, with the dismissal reason as `reason` and `source.kind = "user"` (the user wrote the reason). |
| `tools/port_merge_keep.txt` (7 lines) | entry candidates at `<block>.<path>`, `scope = "subtree"`, the owning-system text as the reason, `keep_mod`. |
| notes #176 and #178 | rule candidates, as in section 4.4, with an empty `reason` and the note text as evidence. The notes are prose, so the tool cannot parse them into rules. |

## 7. Merge mode

### 7.1 Command

```
pdx-audit merge --old 1.3.11 --new 1.4.0-beta [--file PATH | --block NAME] (--dry-run | --apply) [--json]
```

- `--old` and `--new` select tracker versions, as for the audits. `--old` must be a tracked version before `--new`. Otherwise the merge stops with an error.
- `--file` limits the merge to the copies in one mod file. `--block` limits it to one identity.
- `--dry-run` writes nothing in the mod. It prints a unified diff per file and the decision list, and runs the removed-line check.
- `--apply` writes only the files whose removed-line check passes and whose decisions are all closed. It writes atomically and keeps the BOM and the line ends of each file. It needs no format pass: the dry run gives the merged text the layout of pdx-format (section 15).

### 7.2 What it merges

| Copy | base | ours | theirs |
|---|---|---|---|
| REPLACE block | vanilla block at the copy's baseline (`diff3.baseline`), not later than `--old` | the mod block | vanilla block at `--new` |
| INJECT target | vanilla block at `--old` | the injected children | vanilla block at `--new` |
| same-path script file | per top-level definition, as `files.py` splits it | the mod definition | vanilla at `--new` |
| shadowed GUI template or type | vanilla definition at the baseline | the mod definition | vanilla at `--new` |
| same-path GUI file | vanilla file at the baseline | the mod file | vanilla at `--new` |

An INJECT merge touches only the children that the INJECT sets. A vanilla change to another child reaches the game without a merge.

A same-path script file also gets the top-level definitions that vanilla added after `--old` and that the mod lacks. Each goes after the nearest earlier vanilla definition that the mod holds, else before the nearest later one, else at the end. A definition that `--old` held and the mod lacks is a mod deletion: it stays deleted. When vanilla changed it, it is an open decision. A definition that the mod keeps in another file of its folder is not missing.

The intent store decides each new definition, and each deleted definition that vanilla changed. A rule matches it with the change kind `vanilla_added` (or `removed_changed` for a deleted definition) and the path pattern `[""]`, which selects only a deviation of a whole copy. It can also match by `content`, `file`, `block` (the definition name) and `vanilla_key`. `take_vanilla` puts the definition in, `keep_mod` keeps it out, and a grouping rule (no disposition) makes it open. With no rule, a new definition goes in, unless a pdx-maint system or a rule's `content` or `file` field owns the file. Then it is open. With no rule, a deleted definition that vanilla changed is always open.

Text that vanilla did not hold at `--old` merges with an empty base. This applies to a copy, a definition, or a same-path file that vanilla added later. A node that both sides hold alike stays once. A node that only vanilla holds goes in. A node that both sides added in different forms is a `both_added` decision, so one key is never written two times.

Vanilla can remove the text of a copy after `--old`: a REPLACE block, an INJECT target, a GUI definition, a file definition, or a whole same-path file. Each one is a `vanilla_removed` decision. `take_vanilla` deletes the block. A whole file stays open, because `--apply` never deletes a file.

The merge lists each text that it cannot compare node by node, with the reason. The plan holds these in `skipped`:

- a definition too large to compare statement by statement;
- a text that is not script, where vanilla changed lines;
- a REPLACE or an INJECT that the audit could not read;
- a file with no version in the tracker (`file_untracked`: a `.dds` or `.splnet` file that the mod copies from the game);
- a file whose type the tracker did not record at `--old` (`.map` and `.csv` before 1.4.0). Vanilla held the file, but no base is known. An empty base would call each vanilla line an addition.

Excluded: files whose first line holds `AUTO-GENERATED`, and outputs that `pdx-maint.toml` lists for an active tool, by glob or in the tool's `manifest:` file. Their generators read vanilla again. The merge lists them as "regenerate", with the tool name, and never as skipped.

### 7.3 Algorithm

The merge works on nodes, never on lines.

1. Parse base, ours and theirs with `diff3.nodes` (the shared parser, GUI and script). Remove a BOM first. Normalize the keyword case in node signatures only, never in the output text.
2. Align base with ours, and base with theirs, with `diff3.align` in the dialect of the file. Each base node gets an ours state (`same`, `changed`, `deleted`) and a theirs state.
3. Decide each base node on its own:

| ours | theirs | Result |
|---|---|---|
| same | same | ours |
| changed or deleted | same | ours |
| same | changed or deleted | theirs, unless a rule or an entry says `keep_mod` |
| changed | changed, equal to ours | ours |
| changed | changed | a block with the same head: recurse. Otherwise apply the disposition, or open a decision. |
| deleted | changed | apply the disposition, or open a decision |
| changed | deleted | apply the disposition, or open a decision |

4. Insertions. A node that only theirs has goes after the ours counterpart of its nearest earlier base sibling. With none, it goes before the counterpart of its nearest later sibling. With neither, it goes at the end of the block. A node that only ours has stays where it is. A node that both inserted with the same signature is kept once, at the ours position. This prevents the double insertion of v2.
5. Each base node is decided alone, so two adjacent edits never fuse into one conflict. Hunks exist only in the printed diff.
6. GUI templates. A `using = X` line that vanilla adds gives its block the statements of template X. The plan reads the templates of vanilla at `--new`, with the mod's own template in place of vanilla's of the same name. When template X sets a statement that ours sets to another value, the `using` line is an open decision, and so is each statement that vanilla removed because the template now sets it. 1.4 `bg_circle_piechart` moved `texture` into `bg_round_button_alt_texture`, and SUL draws its own texture.
7. Moved and copied blocks. A vanilla block that the mod holds inside a block of its own follows vanilla's change, and goes when vanilla removes it. When the original place in ours still holds as many blocks of that key as vanilla held there, the mod copied the block and did not move it. A removal is then an open decision, never a deletion. When the mod moved a block that vanilla changed, and ours already holds vanilla's new text at the other place, the decision is `keep` and needs no action.
8. Output: the merge splices into the ours text at node offsets (`Node.start`, `Node.end`). The mod's layout and comments stay. An inserted vanilla node takes vanilla's text, re-indented to the depth of its new place. A one-line block of ours that a merged node gives a line break gets one child per line, with tab indents.

### 7.4 Decision list

Each open decision gives: the address, the three texts, the change kind, the matched rules (if two disagree), and the mod commit that wrote the ours node. The commit comes from the line-range `git blame -C -C` of section 6.1.

### 7.5 Removed-line check

The dry run of port_merge v2 caught real errors with this check, so the merge keeps it. Every line that the merged text removes from the ours text must have one of these causes:

- vanilla deleted the node, and ours had it unchanged from base;
- vanilla changed the node, ours had it unchanged from base, and the merge took theirs;
- a `take_vanilla` disposition or a `banned` rename replaced it.

The comparison uses normalized statements, so a layout change is not a removal. Any other removed line fails the file: the dry run prints it, and `--apply` skips the file.

### 7.6 Acceptance test (phase 4)

The brief sets it: a dry run on `in_game/gui/economy_lateralview.gui` and `in_game/gui/map_markers_city.gui`, ours = `git show HEAD:<path>` in the mod repo, `--new fcd7301f`. The result must hold vanilla 1.4's additions (caesar_plotline tax history, `economy_main_tabs` `button_main_tab_alt`, `default_format` `#explanation_link`) and SUL's lines (`using = economy_slider_track_range`). Then a comparison with the hand merge in the working tree, difference by difference. Then one script REPLACE block that port_merge handled. The orchestrator picks the block.

## 8. Lint gate

### 8.1 Rule

A deviation is a lint finding when all of these are true:

- it is new or changed since the store baseline (its deviation id is not in `baseline.deviations`);
- no rule and no `recorded` entry covers it.

Also a finding: each use of a `banned` name, each `stale` or `lost` entry, and each store error (section 3.3).

An old unattributed deviation is exempt until it changes, or until a patch conflicts on it: the copy differs at a node where vanilla changed after the baseline was set.

The deviation id reuses the finding fingerprint of `ledger.finding_id`: kind, target, key path and both normalized texts. Line numbers do not enter it.

### 8.2 Integration

pdx-lint stays generic. The mod gets one check, `tools/lint/port_intent.py` (the brief allows this one new file in the mod). It runs:

```
pdx-audit intent check --json [--changed FILE ...]
```

and returns one string per finding. With `changed`, it checks only the copies in those files, plus the banned names in changed `.gui` files.

### 8.3 Runtime budget

pdx-lint takes about 35 s for the whole mod. The three copy audits take 109 s on the real mod now (measured 2026-10-01, warm tracker caches, 1.3.11 to 1.4.0-beta). So the check cannot run the audits.

A deviation cache solves this: `<tracker cache>/deviations-v1-<vanilla commit>.json`, keyed by (identity, SHA-1 of the copy's text). An unchanged copy costs one hash. Only a changed copy runs `diff3.compare` against the cached block index. Phase 3 measures the cost of a full run against the budget. A full lint run after a patch fills the cache once. The default lint run gets `changed` files only.

Measured on 2026-10-01 (SUL, 33,408 copies, 63,293 deviations, warm caches): a full check takes 19 s, a check of one or two changed files 8 to 10 s. The first collection after a vanilla version or a large mod change takes about 50 s. The lint check file is `contrib/port_intent.py` in this repo, because the mod repo is read only for the tool sessions; the mod session copies it into `tools/lint/`.

## 9. Renames

### 9.1 Detection in the dependency audit

Phase 0 reports a dropped name with the patch that dropped it. Phase 2 adds a measured rename candidate. It does not guess from the spelling. At the patch that dropped name X, the audit aligns vanilla's last version that used X with the next version (the `diff3._History` alignment). At each vanilla site of X, it reads what the aligned node holds now. When one name Y holds at least the threshold share of these sites, the finding detail says: "vanilla replaced it with Y at 12 of 14 sites". Example: `header_action_button_left` became `header_action_button_left_uber` in 1.4.

For script keys the same method applies to the vanilla sites of the key (`local_food_decay_modifier` to `local_food_preservation_efficiency_modifier`). A rename that also changes the structure (`limit` to `family`) or the scale (government power to political influence, x100 for the SUL 0.01 scale) gives no single Y. The audit then reports "no single replacement".

The candidate is evidence. The tool writes no rule from it.

### 9.2 Rename rules

A `banned` rule with a `rename` maps the old name to the new name. `kind` says where the name occurs: `gui_type`, `gui_template`, `binding`, `key`, `value`. The merge rewrites matching mod nodes and lists each rewrite. The lint check reports each remaining use. A rename with a value change has no automatic form. The merge lists it as an open decision.

## 10. CLI and JSON

### 10.1 Commands

```
pdx-audit intent list [--system S] [--state recorded|stale|lost] [--json]
pdx-audit intent show <id>
pdx-audit intent add-rule --file RULE.toml
pdx-audit intent add --finding <finding id> --disposition D --system S --reason "..." [--source note:176] [--scope node|subtree]
pdx-audit intent confirm <entry id>          # take a stale entry as it is now
pdx-audit intent remove <id>
pdx-audit intent check [--changed FILE ...] [--json]
pdx-audit intent baseline --set
pdx-audit intent propose [--system S] [--out PATH]
pdx-audit intent seed [--dismissals] [--keep-file PATH] [--out PATH]
pdx-audit intent accept PROPOSAL [--only c1,c2]
pdx-audit merge --old X --new Y [--file P | --block B] (--dry-run | --apply) [--json]
```

`intent add --finding` takes the id that each audit finding already shows, so the user records intent from the report.

### 10.2 `intent check --json`

```json
{
  "vanilla": "1.4.0-beta",
  "findings": [
    {"type": "unattributed", "deviation": "9b1e04aa", "address": {...},
     "file": "in_game/common/laws/fe_laws.txt", "line": 112, "change": "mod_changed"},
    {"type": "banned", "rule": "gui.no_header_action_button_left",
     "file": "in_game/gui/fe_location_window_types.gui", "line": 2809},
    {"type": "stale", "entry": "e-3fa2c1d0", "side": "vanilla"}
  ],
  "exempt": 120,
  "attributed": 45
}
```

### 10.3 `merge --json`

```json
{
  "old": "1.3.11", "new": "1.4.0-beta",
  "files": [
    {"file": "in_game/gui/economy_lateralview.gui", "copies": 1,
     "taken": 14, "kept": 9, "applied": [{"node": {...}, "by": "rule:integration.fu_marker_rate_vanilla"}],
     "open": [{"address": {...}, "change": "both_changed", "base": "...", "ours": "...",
               "theirs": "...", "commit": "6a08d26", "rules": []}],
     "removed_check": {"passed": true, "unexplained": []},
     "diff": "--- a/...\n+++ b/...\n..."}
  ],
  "regenerate": [{"file": "in_game/common/advances/fe_generated_0_age_of_discovery.txt",
                  "tool": "generate_vanilla_overrides"}]
}
```

## 11. Migration

- A record without `intent` reads as an empty store. No migration step.
- `intent.version` starts at 1. A later schema change bumps it, and pdx-audit converts on read.
- Dismissals stay as they are. Seeding copies them into a proposal. It does not remove them.
- `tools/port_merge.py`, `tools/script_merge.py` and `tools/port_merge_keep.txt` stay in the mod until the merge mode passes its acceptance test. The mod session then retires them in `pdx-maint.toml`.

## 12. Test plan

Synthetic trackers (`conftest.build_tracker`) for each case:

- Store: round trip; a branch sees the intent of its start point; unknown system refused; reason required.
- Address: a repeated `if` told apart by `limit`; two equal selectors told apart by `n`; resolution through one vanilla `limit` change gives `stale`; a block that vanilla moved to another file keeps its identity; a block that the mod moved into an `fe_` file keeps its identity.
- Matcher: each field; keyword case (`not` and `NOT`); BOM on the first key; `REPLACE:` prefix.
- Dispositions: keep_mod over an old vanilla value; take_vanilla; merge recursion; banned with and without rename; entry over rule; two rules in conflict.
- Merge: each row of the table in section 7.3; adjacent edits stay separate; same insertion on both sides kept once; insertion anchors; ordered set lists; GUI positional children; INJECT children only; AUTO-GENERATED files excluded; the removed-line check fails on an unexplained removal.
- Lint: baseline exemption; a changed old deviation becomes a finding; deviation cache hit and miss; `--changed` limits.
- Renames: the measured candidate for a GUI type and for a script key; "no single replacement" for a structural change.
- Real data (manual, not in CI): the acceptance test of section 7.6.

## 13. Estimate

| Phase | Content | Estimate |
|---|---|---|
| 2 | Store schema, address and resolution, matcher, `intent` commands except `check`, seeds, proposer, rename candidates in the deps audit, tests | 3 to 4 working sessions |
| 3 | `intent check`, deviation cache, baseline, mod lint check `port_intent.py`, tests, runtime measurement against the 35 s budget | 1 to 2 sessions |
| 4 | `merge` for REPLACE and same-path script files, then INJECT, then GUI copies; removed-line check; blame cache; acceptance tests on the two GUI files and one script block | 3 to 4 sessions |

The largest risk is in phase 4: the re-indentation of inserted vanilla GUI text, and GUI files where `diff3.align` pairs positional children wrongly. The acceptance test measures both.

## 14. Decisions (2026-10-01)

These answers replace the open questions of the draft. Where they differ from a section above, they apply.

1. **Rules.** The approved rules are: food cancels; `fe_` unique production methods (every building type); specialized rank flags; climate classes. There is no `header_action_button_left` ban: the removed-name check of the dependency audit covers every removed name. A system `faster_universalis` groups all `[FU]` lines. Its rule has no disposition: such nodes always go to the user, and the merge never applies them.
2. **[FU] lines** end in a `# [FU]` comment. The rule matches on the comment.
3. Done in 1.
4. `--apply` refuses a stale entry.
5. The proposer may fill a reason from a pdx-maint note, with the note id as the source. It never fills a reason from a commit message or a code comment. The user is the source of intent.
6. The store is per mod commit, with branch semantics.
7. Rename candidates are review findings only. No tool applies them.
8. **No transform matcher.** A rule has a `tool` field: the pdx-maint registry id of the tool that maintains it. A node that a tool produces (an `AUTO-GENERATED` file, a composer output) is never merged; the report says "regenerate with `pdx-maint run <id>`". pdx-audit never imports mod code. It may call another tool only through the tool's registered command, only in dry-run mode, and only for a tool with a stable job (the composer's rewrites). Otherwise it only names the tool. Section 4.2 is replaced by this.
9. The uncommitted change of `overrides.py` is committed on its own. The template and block check moved from `run_deps_audit` into `gui_names.py`.
10. The dependency audit reads the pdx-syntax database (config key `engine_data`) to drop or confirm binding findings.

General rule: every pdx-audit action that changes mod files has a dry run that shows the full diff, and `--apply` refuses when the removed-line check of the dry run fails. A tool update never changes mod files without a reviewed dry run.

Changes that follow from these decisions:

- A `banned` rule does not rewrite. The merge lists each use as an open decision, and the lint gate reports it.
- A proposal is a JSON file, not TOML, so that pdx-audit needs no TOML writer. A disposition of `""` in a proposal means "not chosen yet", and `accept` refuses it. `null` makes a grouping rule.
- A seed from the keep file or a dismissal carries the text that the user wrote, with the source kind `user`.

## 15. As built (phase 4)

Changes against sections 7 and 10 that the acceptance tests asked for:

- **Pairing.** The merge does not use `diff3.align` alone. Identical nodes pair first, in order. Between them, nodes pair by an order-keeping weighted match: a named block or a block with a selector pairs only with its own; other blocks of one key score by the statements they share, and the base-to-ours score also counts the closeness to vanilla's new node. Moved nodes pair by signature, name, selector, or a key that only one unpaired node holds on each side.
- **Granularity.** A block that ours left as the base and vanilla changed is merged inside, so ours keeps its layout and comments.
- **Comments.** The comment lines directly above a node move with it when the merge deletes or inserts it. An insertion before a sibling goes above the sibling's comments.
- **take_vanilla on a node ours dropped** puts vanilla's node back at its anchor.
- **INJECT.** A key that the INJECT sets and vanilla changed in the window is an `inject_overlap` decision. `keep_mod` keeps it; `take_vanilla` deletes the key from the INJECT, so vanilla's value applies.
- **Removed-line check.** It compares the merged text with ours, less the lines an op wrote or touched, as a multiset of statements. So a moved line is not a removal, and a line that the output lost still shows.
- **Plan.** The dry run saves a plan in the per-user data folder. `--apply` takes the plan and writes exactly its text.
- **Base text in the plan.** Each decision holds the base text in `base` and the base version in `base_version` (`(none)` for an empty base).
- **Comments, extended.** A comment that vanilla writes in the place of a statement it removes takes the statement's place. A changed or inserted vanilla node brings the comment lines above it and the comment after it on its line, unless the mod changed that comment. A node that vanilla did not otherwise change takes vanilla's change to its comments, when the mod left them as the base had them. An insertion into a one-line block puts vanilla's comments on lines of their own.
- **Layout.** The dry run runs pdx-format once on each merged `.txt` and `.gui` file whose mod text is in pdx-format layout already. It checks that the formatted text holds the same tokens and comments. A file that the mod keeps out of pdx-format keeps its own layout, and the report names it. A file that pdx-format refuses fails, and `--apply` refuses it.
- **Empty lines.** The plan splices its edits so that a removal never leaves two empty lines in sequence.
- **Nodes from a later vanilla version.** A copy's base is the version it matches best, but the mod can take a node from a later vanilla version. The merge reads vanilla's versions between the base and `--new`. A node that only ours has, and that one of these versions held as it is, is vanilla's text: vanilla's later change to it is a `vanilla_changed` or `vanilla_removed` decision.
- **Blocks the mod moved.** The mod can move a vanilla block, as it is, into a block of its own. After the pass, the merge finds such copies. When vanilla no longer holds the text, the copy takes vanilla's change or goes, and the original's place gets no second copy. A new vanilla block that holds such a moved block is an open decision, because taking it would write the moved block two times.
- **Changes older than `--old`.** A copy's base is the vanilla version it matches best, which can be older than `--old`. Vanilla's changes between that base and `--old` are changes that an earlier port did not take, missed or left out on purpose. The merge never takes one of them without a rule or an entry: it is an open decision with the reason "vanilla made this change before --old". The test reads the node's level in vanilla at `--old`, by the base address first and then by vanilla's new address. A change is older when that level no longer holds the base node, or holds vanilla's new node already. A rule or an entry still decides such a change, because a rule is the user's intent.
- **Overlaps.** Two edits that overlap fail the removed-line check. A copy that the plan cannot splice, and a new definition whose place falls inside a merged node, are open decisions.
- **Generated files.** The override, files and GUI audits report a generated file as one finding per file when vanilla changed its source in the window. The merge lists it to regenerate. Detection reads the first line, or in a localization file the line after the language key (as the mod's `is_generated`), and names the tool from `pdx-maint.toml`. A file that a tool lists in its `manifest:` output file is that tool's output too.

### 15.1 Open questions

The whole-mod reconciliation of 2026-10-01 (SUL, 1.3.11 to 1.4.0-beta) left these questions for the user:

1. Decided 2026-10-02: a vanilla change older than `--old` is an open decision, never an automatic take (see "Changes older than `--old`" above).
2. Vanilla 1.4 rebuilt location_card and BuildingType_tooltip into new blocks. The merge takes vanilla's new blocks and leaves the mod's changed nodes as open decisions. Must the mod's changes move into vanilla's new blocks?
3. Some rules derive the mod's value from vanilla's value (the food cancels). Must a disposition support this, in addition to `keep_mod`?
