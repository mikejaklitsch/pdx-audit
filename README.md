# pdx-audit

Your mod overrides vanilla files. The game gets a patch, vanilla changes, and your copies do not change. pdx-audit finds these differences. It puts them in order by severity, and it keeps a record of your decisions. It never writes in your mod.

## Install

pdx-audit needs Python 3.10 or later, and git.

```bash
pipx install "pdx-audit @ git+https://github.com/mikejaklitsch/pdx-audit.git"
```

## Start

```bash
pdx-audit --display
```

The window opens on the Settings page when there is no tracker yet. You do the rest of the setup there.

## Set the tracker and the game

On the **Settings** page, fill the two boxes and press **Save** under each one.

- **Game folder**: the `game` folder of your EU5 install. **Browse** opens a file dialog.
- **Tracker**: a folder for the vanilla history. Give a path that does not exist yet, and pdx-audit makes the folder. If you have a tracker already, give its path instead. Any name works.

A git repository that you keep of the game install also works as the tracker. Give the path of its `.git` folder, or of the folder that holds `.git`. pdx-audit reads the game files from the folder of each commit that holds `in_game`, `main_menu` and `loading_screen`, and it skips commits that hold no game files. You commit new versions to that repository with git, because **Commit version** writes only to a tracker that pdx-audit made. pdx-audit only reads that repository, and writes nothing into it or into the install.

## Build the history

Each commit holds the `.txt`, `.yml` and `.gui` files of one game version, read from your install, and the small text formats that mods also replace whole: `.csv`, `.map`, `.shader`, `.fxh` and `.asset`. A tracker made before a format was added holds none of it; commit the installed version again under a new tag (for example `1.3.11.1`) to start its history. The audits compare your mod with these commits. A commit does not read your mod.

**Commit the versions oldest first.** pdx-audit refuses a version that is older than the newest one in the tracker, because the audits read the git order as the patch order. So if you commit the current version first, you cannot add older versions. You must start again with a new tracker.

Decide the oldest version that you want to track. Then do these steps on the **Tracker** page:

1. In Steam, open the Properties of the game. On the Betas tab, select that version.
2. Let Steam update the files.
3. Type the version in **Version**, then press **Commit version**.
4. Do steps 1 to 3 again for each newer version, in order.
5. Select the current version last, and commit it.

The audits need two commits or more. Make one more commit after each game patch. The app shows a note when the game has changed since the newest commit.

## Read the findings

Press **Run audits**. Each chip in the top bar gives the number of findings of one audit. Press a chip to show or hide its findings. Select a finding to see your statement against the change that vanilla made. For a REPLACE or a GUI copy, the app also shows the current text of vanilla next to your copy.

**Dismiss** hides a finding that is correct as it is. A dismissal applies until the text of one side changes. The **Dismissed** page brings one back.

**Open in editor** opens the finding's file at its line. Set the `editor` setting to the command of your editor, with `{file}` and `{line}` where it wants them, for example `myeditor --goto {file}:{line}`. With no setting, the system's default program opens the file.

All the changes that vanilla made in one block are one finding. For a REPLACE, the **REPLACE findings** setting on the Settings page selects this: `block` gives one finding for each REPLACE, and `statement` gives one finding for each change. `pdx-audit --set replace_findings statement` sets the same value. The change applies from the next run, and a dismissal made in one mode does not apply in the other.

[HOW_IT_WORKS.md](HOW_IT_WORKS.md) gives what each audit compares, and why it reports what it reports.

## The command line

The app runs the audits through this interface, and you can use it to script them. Run pdx-audit in your mod folder, or in a folder below it. It finds the mod root by the `.metadata` folder, and `--mod-root` gives the root directly.

```bash
pdx-audit                     # all six audits
pdx-audit --overrides         # REPLACE and INJECT blocks against vanilla
pdx-audit --deps              # names, GUI types and templates, and data-binding names that vanilla no longer uses
pdx-audit --gui               # vanilla GUI changes that your copies do not have
pdx-audit --files             # vanilla changes inside files your mod replaces at the same path
pdx-audit --loc               # vanilla strings that changed under keys you override
pdx-audit --dupes             # names that the mod defines in more than one place

pdx-audit --summary           # the summary only, with no detail
pdx-audit --diff              # show the line changes
pdx-audit --full              # from the oldest commit, not the last patch
pdx-audit --commit 1.3.12     # record the installed game as a version
```

Each finding starts with an id, which `--dismiss` takes:

```
  - [cc01a5e3] `some_building` `in_game/common/building_types/my_buildings.txt:4` modifier
      yours:    cost = 100
      vanilla:  cost = 100  →  cost = 200  (1.3.11)
```

```bash
pdx-audit --dismiss cc01a5e3 --reason "halved on purpose"
pdx-audit --show-dismissed
pdx-audit --undismiss cc01a5e3
```

`pdx-audit --set` writes the settings that the Settings page writes: `vanilla_repo`, `game_root`, `patch_name`, `replace_findings`, `merge_default` and `editor`. Edit `skip_dirs`, `skip_files`, `merge_types` and `engine_data` in the config file. `engine_data` is the path of the pdx-syntax database (`eu5_syntax.db`); the dependency audit reads it to confirm or drop its data-binding findings. `pdx-audit --config` shows each setting, where it comes from, and where the file is.

## Record why the mod differs

A dismissal hides one finding. The intent store explains many differences at once, and a later merge applies it. pdx-audit keeps the store in your per-user record, beside the dismissals. It never writes it into the mod.

- A **rule** belongs to a system of `pdx-maint.toml`. It has one reason, a source (a pdx-maint note, a commit, or you), a disposition, and a matcher that selects differences by key, value, path, comment, change and file. A rule without a disposition is a grouping: its differences always come to you.
- An **entry** explains one difference, or one block with all that is in it.
- A **disposition** is `keep_mod`, `take_vanilla`, `merge` or `banned`.

```bash
pdx-audit intent propose                     # candidates for the differences that nothing explains
pdx-audit intent seed --dismissals --keep-file tools/port_merge_keep.txt --rules rules.json
pdx-audit intent accept <proposal.json> --only c1,c4
pdx-audit intent list [--state stale]
pdx-audit intent add --finding cc01a5e3 --disposition keep_mod --system economy --reason "halved on purpose"
pdx-audit intent add-rule rule.json
pdx-audit intent confirm <entry id>          # take a stale entry as the node reads now
pdx-audit intent remove <id>
```

### The lint gate

```bash
pdx-audit intent baseline --set              # exempt the differences that nothing explains now
pdx-audit intent check [--changed FILE ...]  # differences that are new since, with no rule or entry
```

A difference that no rule and no recorded entry explains is a finding when it is new or changed since the baseline, or when vanilla changed it after the baseline. A banned use, a stale or lost entry and two rules that disagree are findings too. `contrib/port_intent.py` runs the check as a pdx-lint check: copy it into the `tools/lint/` folder of the mod. A cache keeps the differences of each copy, so a check with `--changed` compares only the copies in those files.

A proposal is a JSON file in the per-user data folder. The proposer fills a reason only from a pdx-maint note, and gives the note id as the source. It shows commits and comments as evidence only. Write the reason, the system and the disposition of each candidate in the file, then accept it.

## Merge a patch into your copies

```bash
pdx-audit merge --old 1.3.11 --new 1.4.0 [--file PATH | --block NAME] --dry-run [--choices FILE] [--saved] [--choose accept|keep]
pdx-audit merge --old 1.3.11 --new 1.4.0 --apply <plan.json> [--file PATH] [--choices FILE] [--saved] [--choose accept|keep]
pdx-audit merge --old 1.3.11 --new 1.4.0 --clear-saved
```

The merge takes the changes of vanilla into your REPLACE blocks, your same-path files and your GUI copies, node by node, and keeps your own edits. The intent store decides where both changed: a rule or an entry with `keep_mod` keeps yours, `take_vanilla` takes vanilla's. Each other conflict is an open decision, with the commit that wrote your line. The dry run keeps the `git blame` of each file in the mod's data folder, so the next dry run blames only the files that changed or that a new commit changed. For an INJECT, a key that you inject and that vanilla changed is a decision.

The `merge_default` setting gives the action for a change that only vanilla made: `ask` (the default) makes it an open decision, `accept` accepts it, and `keep` keeps your line. A rule or an intent store entry still decides the changes that it names. Each open decision gives its cause: **Merge Conflict** (you and vanilla both changed the text), **Needs Review** (the merge cannot accept vanilla's change safely, for example a change that vanilla made before `--old`), or **No Conflict** (only vanilla changed the text, and `merge_default` is `ask`).

The dry run writes nothing in the mod. It prints the full diff and the decisions, checks that every line it removes is explained by a change of vanilla, and saves a plan. `--apply` writes the plan: it refuses a file that changed after the dry run, a file whose removed-line check failed, a file with an open decision, and a file that a stale entry decided. It also refuses a plan of other versions than `--old` and `--new`. After the write, `--apply` reads each file back and plans it again with the same versions and the same base version for each copy. A change that the file took must not be a decision again, and each definition that takes a change must still be in the file. A file that fails gets its old bytes back, and the report names each change. So the same merge again on a written file writes nothing. A file that a tool generates is never merged: the report names the tool to run. The dry run gives each merged file the layout of pdx-format, so the files that `--apply` writes need no format pass. A file that you keep out of pdx-format keeps its own layout.

The merge also adds the top-level definitions that vanilla added to your same-path files. The intent store decides each one: `take_vanilla` puts it in, `keep_mod` keeps it out, and a rule with no disposition makes it open. With no rule it goes in, unless a pdx-maint system owns the file. Text that vanilla added after `--old` merges with an empty base. Text that vanilla removed is a `vanilla_removed` decision. A vanilla change older than `--old`, which an earlier port did not take, is an open decision: the merge takes only the changes of this window by itself. A file that the merge cannot compare node by node (too large, not script, no vanilla history) is listed with the reason.

Your choices change single decisions. In a choices file, `take` accepts vanilla's change, and `keep` keeps your line. `--choices` names a JSON file of choices, `{"files": {"<file>": {"sha": "<sha1 of the file text>", "all": "take", "nodes": {"<choice key>": "keep"}}}}`. The choice key of a decision is `[identity, path, kind]` of its address, in JSON. A file can also hold your custom merges, `"own": [{"span": [<start>, <end>], "text": "<text>", "edges": [true, true]}]`: each one takes the place of that span of the file, the block that holds a change. The text goes in as it stands. `edges` tells whether the text touches the blank lines above and below it, which the merge makes one empty line next to an edit; the app sets it so that the text as Apply file writes it gives the same file again. Each decision with an edit in the span then takes your text, and its own choice does not apply. When the text does not close its blocks, or an edit of the merge crosses an edge of the span, the text does not go in and its decisions are open. `all` is optional: it is the choice for each decision of the file that `nodes` does not name. `--choose accept` or `--choose keep` gives that choice to every file of the run; use it with `--file` for one file. A choice lapses when its file changes. `--saved` uses the choices that the app saved for `--old` and `--new`; `--choices` names the files that it changes. `--clear-saved` deletes them. The plan holds the result of each choice, so `--apply` with `--choices` or `--choose` makes each file again from the plan with these choices, and needs no new dry run. Some decisions take no choice or only one: `--apply` does not delete a file, and a block that the merge cannot read stays with you. The dry run gives the number of decisions that `--choose` did not set, and the reason for each group.

The **Merge** page of the app runs the same merge. **Plan merge** makes the dry run and lists each file: ready files first, then the files with open decisions. Each decision has its own row, in the order of the file. The ✓ button of a row shows what Apply file does with that node: **Accept Vanilla Change** or **Keep My Line**. An open row has no ✓, and its mark gives its cause. Click the other button to record your choice for that one node. **Write Custom Merge** opens an editor for the whole block that holds the change, the block that the bottom view shows. Your text takes the place of that block, with each change in it. The editor starts from the block as Apply file writes it. **Start from mine**, **Start from vanilla** (your block with each vanilla change in it) and **Start from the result** copy that text into it. Each row in the block then shows **✓ Custom Merge**, and its other buttons are off. **Remove Custom Merge** in the editor uses your choices for the block again. **Accept All Vanilla Changes** and **Keep All My Lines** choose for each row of the file, but do not remove your custom merges. **Clear my choices** goes back to the defaults. A row choice that you make after a choice for all stays. The row shows a choice at once, and the file, its state and the view follow within a moment: the page makes the file from the plan with the code of the dry run, and runs no command. A button that no choice can use is off, and the row and the button's tooltip say why. When a choice for all does not apply to a decision, the file note gives the number of such decisions and the reason for each group. Click a row to see the full block that holds it below the rows. The left side shows the options: your lines in red, with vanilla's text for each change in green below them. The right side shows the outcome: the file after Apply file, with the lines that come from vanilla in green. The selected decision is outlined. A decision for a node that your file does not hold shows the line near which the merge puts it. With no row selected, the view shows the whole file and folds the unchanged lines. **Apply file** writes a file once nothing in it is open, and **Apply all ready files** writes every ready file. The checks of `--apply` stay, and it writes a file only when its text is the text that the page shows. The page saves your choices after each change, in the mod's data folder, for the versions of the plan. The next **Plan merge** for these versions uses them again, also after the app closes. **Clear saved choices** deletes them in every file. Each command reads the plan and the choices from a temporary file that the page deletes when the command ends (`merge --apply --choices`). A choice lapses when its file changes. The file list has a search box, a filter and a sort order.

## From a clone

Clone [pdx-utilities](https://github.com/mikejaklitsch/pdx-utilities) next to this repo: pdx-audit's shared helpers live there. Then run `./pdx-audit`, or install with `pipx install --editable .` followed by `pipx inject pdx-audit --editable ../pdx-utilities`, so edits to the shared helpers reach pdx-audit immediately. The tests run with `python -m pytest`.
