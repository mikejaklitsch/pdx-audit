"""The desktop app's side of the command line: a run writes its findings to a
results file, dismissals made from those records match the terminal's ids, and
the run options turn into the same flags a user would type."""
import json
import sys

import pytest

from pdxaudit import results
from pdxaudit.safety import remove_file

from test_cli import cli  # noqa: F401  (fixture)


def _payload(cli, *argv):
    path = cli.data.parent / "results.json"
    code, _out, err = cli(*argv, "--results-file", str(path))
    assert code == 0, err
    return json.loads(path.read_text(encoding="utf-8"))


def _records(payload, name):
    return [r for r in payload["records"] if r["name"] == name]


def test_results_file_holds_every_actionable_finding(cli):
    payload = _payload(cli)
    assert payload["mod"] and payload["new_tag"] == "1.1.0"
    assert payload["triage"].startswith("# Audit summary")
    assert {audit for audit, _text in payload["details"]} >= {"overrides", "gui", "loc"}
    kinds = {r["kind"] for r in payload["records"]}
    assert "override_replace_new_line" in kinds and "loc_changed" in kinds
    assert isinstance(payload["info"], int) and payload["dismissed"] == 0
    for r in payload["records"]:
        assert r["id"] == r["fid"][:8]


def test_every_replace_line_shares_its_block_view(cli):
    lines = _records(_payload(cli, "--overrides"), "some_building")
    assert len(lines) >= 2
    blocks = {r["block"] for r in lines}
    assert len(blocks) == 1
    payload = _payload(cli, "--overrides")
    assert payload["blocks"][blocks.pop()]["patch"]


def test_replace_lines_carry_the_yours_vanilla_pair(cli):
    lines = _records(_payload(cli, "--overrides"), "some_building")
    new_line = next(r for r in lines if r["kind"] == "override_replace_new_line")
    assert new_line["yours"] == "(missing)"
    assert new_line["vanilla"].startswith("added ") and "upkeep" in new_line["vanilla"]


def test_gui_findings_carry_the_definition_lined_up_against_vanilla(cli):
    foo = next(r for r in _records(_payload(cli, "--gui"), "foo")
               if r["kind"].startswith("gui_shadow"))
    assert foo["data"]["patch"] == [
        {"t": "template foo = {", "c": "context"},
        {"t": "\tsize = { 10 10 }", "c": "changed", "e": [[10, 15]]},
        {"t": "\tsize = { 20 20 }", "c": "new", "e": [[10, 15]]},
        {"t": "}", "c": "context"}]
    assert foo["data"]["vanilla_file"] == "in_game/gui/vanilla.gui"


def test_vanillas_lines_take_the_indentation_of_the_copy():
    from pdxaudit.gui import _gui_patch
    mod = "template foo = {\n\t\tblock = {\n\t\t\ttext = a\n\t\t}\n\t\tsize = 1\n}"
    new = "template foo = {\n\tblock = {\n\t\traw_text = b\n\t\ttooltip = {\n\t\t\tname = c\n\t\t}\n\t}\n\tsize = 1\n}"
    rows = _gui_patch(mod, new, missing={"raw_text = b", "tooltip =", "name = c"}, kept={"text = a"})
    assert ("\t\t\ttext = a", "changed") in [(r["t"], r["c"]) for r in rows]
    ghosts = {r["t"].strip(): r["t"] for r in rows if r["c"] in ("new", "added")}
    assert ghosts["raw_text = b"] == "\t\t\traw_text = b"
    assert ghosts["tooltip = {"] == "\t\t\ttooltip = {"
    assert ghosts["name = c"] == "\t\t\t\tname = c"


def test_gui_rows_mark_in_the_findings_severity():
    rec = {"line": "1", "since": "1.1", "sev": "review",
           "data": {"patch": [{"t": "a = 1", "c": "context"}, {"t": "b = 2", "c": "added"}]}}
    assert [(r["mark"], r["sign"]) for r in results.gui_rows(rec)] == [(None, None), ("review", "+")]


def test_gui_rows_keep_indentation_and_sign_vanillas_changes(cli):
    foo = next(r for r in _records(_payload(cli, "--gui"), "foo")
               if r["kind"].startswith("gui_shadow"))
    rows = results.gui_rows(foo)
    assert [(r["n"], r["text"], r["ghost"]) for r in rows] == [
        (1, "template foo = {", False), (2, "\tsize = { 10 10 }", False),
        (None, "\tsize = { 20 20 }", True), (3, "}", False)]
    assert [(r["mark"], r["sign"], r["note"], r.get("emph")) for r in rows if r["mark"]] == [
        ("stale", "-", "", [(10, 15)]), ("stale", "+", "", [(10, 15)])]


# (what the case shows, your copy, vanilla's copy, lines vanilla added, lines vanilla
# dropped, the rows marked: text, class, highlighted ranges)
GUI_PATCH_CASES = [
    ("a key vanilla renamed into a new block, with a plain value, stays unpaired",
     "t = {\n\tonpressed = x\n}", "t = {\n\taction = {\n\t\ton_action = x\n\t}\n}",
     {"action =", "on_action = x"}, {"onpressed = x"},
     [("\tonpressed = x", "changed", []), ("\taction = {", "new", []), ("\t\ton_action = x", "new", [])]),
    ("a distinctive value carried to a new key in a new block pairs the two keys",
     't = {\n\tonpressed = "[OnPause]"\n}',
     't = {\n\taction_tooltip = {\n\t\ttitle = "[Resume]"\n\t\ton_action = "[OnPause]"\n\t}\n}',
     {"action_tooltip =", 'title = "[Resume]"', 'on_action = "[OnPause]"'}, {'onpressed = "[OnPause]"'},
     [('\tonpressed = "[OnPause]"', "changed", [(1, 10)]), ("\taction_tooltip = {", "new", []),
      ('\t\ttitle = "[Resume]"', "new", []), ('\t\ton_action = "[OnPause]"', "new", [(2, 11)])]),
    ("when two blocks line up, what changed inside them is highlighted",
     't = {\n\ttooltipwidget = {\n\t\tLeftClick = {\n\t\t\ttext = "[Resume]"\n\t\t}\n\t}\n}',
     "t = {\n\ttooltipwidget = { Basic = {} }\n}", {"Basic ="}, {"LeftClick =", 'text = "[Resume]"'},
     [("\ttooltipwidget = {", "changed", []), ("\t\tLeftClick = {", "changed", []),
      ('\t\t\ttext = "[Resume]"', "changed", []), ("\ttooltipwidget = { Basic = {} }", "new", [(19, 29)])]),
    ("your value where vanilla changed the same key is a change",
     "t = {\n\tvisible = mine\n}", "t = {\n\tvisible = theirs\n}", {"visible = theirs"}, set(),
     [("\tvisible = mine", "changed", [(11, 15)]), ("\tvisible = theirs", "new", [(11, 17)])]),
    ("your key standing where vanilla changed its key is a change",
     "t = {\n\tlist_a = {\n\t\tx = 1\n\t}\n}", "t = {\n\tlist_b = {\n\t\tx = 1\n\t}\n}", {"list_b ="}, set(),
     [("\tlist_a = {", "changed", [(1, 7)]), ("\tlist_b = {", "new", [(1, 7)])]),
    ("the same key nested deeper by your own wrapper block still pairs",
     "t = {\n\tbox = {\n\t\tdatamodel = mine\n\t}\n}", "t = {\n\tdatamodel = theirs\n}",
     {"datamodel = theirs"}, set(),
     [("\t\tdatamodel = mine", "changed", [(14, 18)]), ("\tdatamodel = theirs", "new", [(13, 19)])]),
    ("a one-line block lines up with vanilla's block spread over lines",
     "t = {\n\ticon = { size = { 22 31 } texture = a }\n}",
     "t = {\n\ticon = {\n\t\tsize = { 31 31 }\n\t\ttexture = a\n\t}\n}", {"31 31"}, {"22 31"},
     [("\ticon = { size = { 22 31 } texture = a }", "changed", [(19, 21)]), ("\ticon = {", "new", []),
      ("\t\tsize = { 31 31 }", "new", [(11, 13)]), ("\t\ttexture = a", "new", [])]),
    ("vanilla's lines indent from your first line with text",
     "t = {\n\n\t\t\tonpressed = x\n}", "t = {\n\tname = n\n}", {"name = n"}, {"onpressed = x"},
     [("\t\t\tonpressed = x", "changed", [(3, 12), (15, 16)]), ("\t\t\tname = n", "new", [(3, 7), (10, 11)])]),
    ("a block whose values are only laid out differently is not marked",
     "t = {\n\tcolor = {\n\t\t0.0\n\t\t1.0\n\t}\n}", "t = {\n\tcolor = { 0.0 1.0 }\n}",
     {"0.0 1.0"}, {"0.0", "1.0"}, []),
    ("distinctive values found once on each side pair even where vanilla swapped their order",
     't = {\n\tonclick = "[Toggle]"\n\ttext = "[Label]"\n}',
     't = {\n\taction_tooltip = {\n\t\ttitle = "[Label]"\n\t\ton_action = "[Toggle]"\n\t}\n}',
     {"action_tooltip =", 'title = "[Label]"', 'on_action = "[Toggle]"'},
     {'onclick = "[Toggle]"', 'text = "[Label]"'},
     [('\tonclick = "[Toggle]"', "changed", [(1, 8)]), ('\ttext = "[Label]"', "changed", [(1, 5)]),
      ("\taction_tooltip = {", "new", []), ('\t\ttitle = "[Label]"', "new", [(2, 7)]),
      ('\t\ton_action = "[Toggle]"', "new", [(2, 11)])]),
]


def test_gui_rows_mark_and_pair_vanillas_changes():
    from pdxaudit.gui import _gui_patch
    for case, mod, new, missing, kept, marked in GUI_PATCH_CASES:
        rows = [(r["t"], r["c"], [tuple(x) for x in r.get("e", [])]) for r in _gui_patch(mod, new, missing, kept)]
        assert [r for r in rows if r[1] != "context"] == marked, case


def test_duplicate_findings_point_at_each_definition(cli):
    dupe = _records(_payload(cli, "--dupes"), "dup_thing")[0]
    assert dupe["data"]["sources"] == [
        {"how": "definition", "file": "in_game/common/buildings/dup1.txt", "line": 1},
        {"how": "definition", "file": "in_game/common/buildings/dup2.txt", "line": 1}]


def test_source_rows_read_each_definition_from_the_mod(cli):
    dupe = _records(_payload(cli, "--dupes"), "dup_thing")[0]
    rows = results.source_rows(dupe, cli.world.mod)
    assert [r["toggle"] for r in rows] == ["definition · in_game/common/buildings/dup1.txt:1",
                                           "definition · in_game/common/buildings/dup2.txt:1"]
    assert all(r["open"] is False for r in rows)
    assert [(c["n"], c["text"]) for c in rows[1]["rows"]] == [
        (1, "dup_thing = {"), (2, "\tcost = 2"), (3, "}")]


def test_a_definition_that_moved_since_the_run_says_to_run_again(cli):
    dupe = _records(_payload(cli, "--dupes"), "dup_thing")[0]
    path = cli.world.mod / "in_game/common/buildings/dup2.txt"
    path.write_text("\n\n" + path.read_text(encoding="utf-8"), encoding="utf-8")
    rows = results.source_rows(dupe, cli.world.mod)[1]["rows"]
    assert len(rows) == 1 and "run the audits again" in rows[0]["note"]


def test_toggles_never_fold_together():
    toggles = [{"toggle": f"definition · f{i}.txt:1", "open": False, "rows": []} for i in range(12)]
    assert results.fold_rows(toggles) == toggles


def test_a_run_records_the_mod_files_it_saw(cli):
    files = _payload(cli)["files"]
    assert "in_game/common/buildings/dup1.txt" in files
    assert "in_game/localization/english/m_l_english.yml" in files
    assert ".metadata/metadata.json" not in files


def test_changed_files_lists_edits_additions_and_removals(cli):
    payload = _payload(cli)
    mod = cli.world.mod
    assert results.changed_files(payload, mod) == []
    (mod / "in_game/common/buildings/dup2.txt").write_text("dup_thing = {\n\tcost = 22\n}\n")
    (mod / "in_game/common/buildings/new.txt").write_text("new_thing = { }\n")
    remove_file(mod / "in_game/common/buildings/dup1.txt", mod / "in_game/common/buildings", r"dup1\.txt")
    assert results.changed_files(payload, mod) == [
        "in_game/common/buildings/dup1.txt", "in_game/common/buildings/dup2.txt",
        "in_game/common/buildings/new.txt"]


def test_loc_findings_carry_the_three_values(cli):
    key = next(r for r in _records(_payload(cli, "--loc"), "KEY_A")
               if r["kind"] == "loc_changed")["key"]
    assert (key["old"], key["new"], key["mod"]) == ("old text", "new text", "mod text")


def test_dismiss_from_records_hides_the_finding_in_the_terminal(cli):
    rec = next(r for r in _records(_payload(cli, "--overrides"), "some_building"))
    done, errors = results.dismiss(cli.world.mod, _payload(cli, "--overrides")["records"],
                                   [rec["fid"]], "kept on purpose")
    assert not errors and len(done) == 1
    code, out, _ = cli("--overrides")
    assert f"[{rec['id']}]" not in out and "1 dismissed" in out
    entries = results.dismissed_entries(cli.world.mod)
    assert [(e["id"], e["reason"]) for e in entries] == [(rec["id"], "kept on purpose")]


def test_undismiss_brings_it_back(cli):
    payload = _payload(cli, "--overrides")
    rec = _records(payload, "some_building")[0]
    results.dismiss(cli.world.mod, payload["records"], [rec["fid"]], None)
    removed, errors = results.undismiss(cli.world.mod, [rec["fid"]])
    assert not errors and len(removed) == 1
    assert results.dismissed_entries(cli.world.mod) == []
    _code, out, _ = cli("--overrides")
    assert f"[{rec['id']}]" in out


def test_duplicates_cannot_be_dismissed_from_records(cli):
    payload = _payload(cli, "--dupes")
    dupe = _records(payload, "dup_thing")[0]
    assert dupe["dismissible"] is False
    done, errors = results.dismiss(cli.world.mod, payload["records"], [dupe["fid"]], None)
    assert not done and "cannot be dismissed" in errors[0]


def test_app_runs_cover_all_five_audits():
    assert results.run_argv({}) == []
    assert results.run_argv({"old": "1.0.0", "new": "1.1.0", "full": False, "block": "",
                             "category": ""}) == ["--old", "1.0.0", "--new", "1.1.0"]
    assert results.run_argv({"full": True, "block": "some_building"}) == [
        "--full", "--block", "some_building"]


def test_a_category_run_covers_the_audits_category_applies_to():
    assert results.run_argv({"category": "common/building_types"}) == [
        "--overrides", "--dupes", "--category", "common/building_types"]


def test_run_options_run_through_the_cli(cli):
    for opts in ({"old": "1.0.0", "new": "1.1.0"}, {"category": "common/building_types"},
                 {"block": "some_building"}):
        assert _payload(cli, *results.run_argv(opts))["records"], opts


def test_run_menu_lists_the_categories_and_blocks_the_mod_overrides(world):
    targets = results.override_targets(world.mod)
    assert targets["common/building_types"] == ["some_building"]
    assert targets["common/script_values"] == ["my_value"]


def _block_payload(patch, records, extra=None):
    data = {"type": "REPLACE", "patch": patch, "absent": [], "removed_note": [], "diff": "",
            "lines": [], "missing": [], "kept": [], "overlap": []}
    data.update(extra or {})
    recs = [dict({"block": "b1", "line": "10", "since": "1.3.8", "audit": "override",
                  "fid": f"{i:040d}", "id": f"{i:08d}", "name": "x"}, **r)
            for i, r in enumerate(records, 1)]
    return {"blocks": {"b1": data}, "records": recs}, recs


def test_block_rows_sign_vanillas_changes_on_the_mod_lines(cli):
    payload = _payload(cli, "--overrides")
    rec = _records(payload, "some_building")[0]
    rows, footer = results.block_rows(payload, rec)
    legacy = next(r for r in rows if "legacy_mod" in r["text"])
    assert (legacy["mark"], legacy["sign"], legacy["ghost"], legacy["note"]) == ("stale", "-", False, "")
    upkeep = next(r for r in rows if "upkeep" in r["text"])
    assert (upkeep["mark"], upkeep["sign"], upkeep["ghost"], upkeep["n"], upkeep["note"]) == (
        "stale", "+", True, None, "")
    assert [r["n"] for r in rows if not r["ghost"]] == [1, 2, 3, 4]
    assert not any(r["mark"] for r in rows if "cost" in r["text"])
    assert footer == []


def test_a_kept_old_value_shows_vanillas_new_value_right_under_it():
    patch = [{"t": "REPLACE:x = {", "c": "context"}, {"t": "\trequires = a", "c": "removed"},
             {"t": "\tother = 1", "c": "context"}, {"t": "\trequires = b", "c": "add"},
             {"t": "}", "c": "context"}]
    payload, recs = _block_payload(patch, [{"kind": "override_replace_frozen", "key": {
        "path": [], "slot": "requires", "op": "=", "old": "a", "new": "b", "mod": "a"}}])
    rows, _footer = results.block_rows(payload, recs[0])
    assert [(r["text"], r["sign"]) for r in rows] == [
        ("REPLACE:x = {", None), ("\trequires = a", "-"), ("\trequires = b", "+"),
        ("\tother = 1", None), ("}", None)]
    assert [r["n"] for r in rows] == [10, 11, None, 12, 13]
    assert rows[1]["emph"] == rows[2]["emph"] == [(12, 13)]
    assert rows[1]["note"] == "" and rows[2]["mark"] == "stale"


def test_notes_name_the_patch_only_when_it_differs_from_the_selected_finding():
    patch = [{"t": "REPLACE:x = {", "c": "context"}, {"t": "\ta = 1", "c": "removed"},
             {"t": "\tb = 2", "c": "removed"}, {"t": "}", "c": "context"}]
    payload, recs = _block_payload(patch, [
        {"kind": "override_replace_kept_removed", "since": "1.3.8",
         "key": {"path": [], "slot": "a", "op": "=", "old": "1", "new": None, "mod": "1"}},
        {"kind": "override_replace_kept_removed", "since": "1.3.6-beta",
         "key": {"path": [], "slot": "b", "op": "=", "old": "2", "new": None, "mod": "2"}}])
    notes = [r["note"] for r in results.block_rows(payload, recs[0])[0] if r["note"]]
    assert notes == ["deleted in 1.3.6-beta"]


def test_a_key_vanilla_deleted_that_you_changed_is_marked_for_review():
    patch = [{"t": "REPLACE:x = {", "c": "context"}, {"t": "\tleft = {", "c": "context"},
             {"t": "\t\tfort = 0.5 # mine", "c": "mine"}, {"t": "\t}", "c": "context"},
             {"t": "}", "c": "context"}]
    payload, recs = _block_payload(patch, [{"kind": "override_replace_both_changed",
                                            "since": "1.3.6-beta", "key": {
        "path": ["left"], "slot": "fort", "op": "=", "old": "0.4", "new": None, "mod": "0.5"}}])
    row = results.block_rows(payload, recs[0])[0][2]
    assert (row["mark"], row["sign"]) == ("review", "-")
    assert row["note"] == "vanilla deleted this key (it was 0.4); you changed it to 0.5"


def test_a_value_you_and_vanilla_both_changed_shows_vanillas_value_under_yours():
    patch = [{"t": "REPLACE:x = {", "c": "context"}, {"t": "\tfort = 0.5 # mine", "c": "mine"},
             {"t": "}", "c": "context"}]
    payload, recs = _block_payload(patch, [{"kind": "override_replace_both_changed", "key": {
        "path": [], "slot": "fort", "op": "=", "old": "0.4", "new": "0.3", "mod": "0.5"}}])
    rows = results.block_rows(payload, recs[0])[0]
    assert [(r["text"], r["mark"], r["sign"]) for r in rows[1:3]] == [
        ("\tfort = 0.5 # mine", "review", "-"), ("\tfort = 0.3", "review", "+")]
    assert (rows[1]["note"], rows[2]["note"]) == ("", "vanilla had 0.4 before")
    assert rows[2]["emph"] == [(8, 11)]


def test_word_spans_mark_only_the_words_that_differ():
    yours = '\t\tvisible = "[And(Setting.ShowModifier, EqualTo(Setting.GetName))]"'
    vanilla = '\t\tvisible = "[And(Setting.ShowModifier, Setting.IsFort)]"'
    spans_y, spans_v = results.word_spans(yours, vanilla)
    assert [yours[s:e] for s, e in spans_y] == ["EqualTo(Setting.GetName)"]
    assert [vanilla[s:e] for s, e in spans_v] == ["Setting.IsFort"]
    yours, vanilla = '\ttext = "x"', '\ttext = "completely different"'
    spans_y, spans_v = results.word_spans(yours, vanilla)
    assert [yours[s:e] for s, e in spans_y] == ["x"]
    assert [vanilla[s:e] for s, e in spans_v] == ["completely different"]
    yours, vanilla = "x = And(A, B, C)", "x = And(A, C)"
    spans_y, spans_v = results.word_spans(yours, vanilla)
    assert [yours[s:e] for s, e in spans_y] == ["B,"] and spans_v == []   # trimmed of the space after it


def test_changes_that_cannot_be_placed_are_listed_under_the_block():
    patch = [{"t": "REPLACE:x = {", "c": "context"}, {"t": "}", "c": "context"}]
    payload, recs = _block_payload(
        patch, [{"kind": "override_replace_new_line", "key": {
            "path": [], "slot": "sub", "op": None, "old": None, "new": None, "mod": None,
            "text": "sub = { … } (3 lines)"}}],
        {"absent": [{"line": "other > k = 1", "block": "other", "vanilla": "", "related": None}],
         "removed_note": ["gone = 1"]})
    rows, footer = results.block_rows(payload, recs[0])
    ghost = next(r for r in rows if r["ghost"])
    assert ghost["text"].strip() == "sub = { … } (3 lines)" and ghost["mark"] == "stale"
    assert rows[-1]["text"] == "}"
    assert [lines for _heading, lines in footer] == [["other > k = 1"], ["gone = 1"]]


def test_long_unchanged_stretches_fold():
    rows = [{"n": i, "text": f"k{i} = 1", "mark": None, "ghost": False, "note": ""}
            for i in range(40)]
    rows[20]["mark"] = "stale"
    folded = results.fold_rows(rows)
    folds = [r for r in folded if "fold" in r]
    assert folds
    assert sum(len(r["fold"]) for r in folds) + len(folded) - len(folds) == 40
    i = next(j for j, r in enumerate(folded) if r.get("mark"))
    assert all("fold" not in r for r in folded[i - 2:i + 3])


def test_a_stretch_that_reaches_the_block_edge_folds_all_the_way_to_it():
    rows = [{"n": i, "text": f"k{i} = 1", "mark": None, "ghost": False, "note": ""}
            for i in range(30)]
    rows[10]["mark"] = "stale"
    folded = results.fold_rows(rows)
    assert "fold" in folded[0] and "fold" in folded[-1]
    i = next(j for j, r in enumerate(folded) if r.get("mark"))
    assert [r["n"] for r in folded[i - 3:i]] == [7, 8, 9]
    assert [r["n"] for r in folded[i + 1:i + 4]] == [11, 12, 13]


def test_display_rejects_command_flags(cli):
    code, _out, err = cli("--display", "--show-dismissed")
    assert code == 2 and "--display" in err


def test_display_without_pyside6_explains_the_install(cli, monkeypatch):
    monkeypatch.setitem(sys.modules, "pdxaudit.app", None)
    code, _out, err = cli("--display")
    assert code == 1 and "PySide6" in err


def test_results_file_never_writes_into_the_mod(cli):
    from test_cli import _files
    before = _files(cli.world.mod)
    payload = _payload(cli)
    results.dismiss(cli.world.mod, payload["records"], [payload["records"][0]["fid"]], None)
    assert _files(cli.world.mod) == before
