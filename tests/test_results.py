"""The desktop app's side of the command line: a run writes its findings to a
results file, dismissals made from those records match the terminal's ids, the
block view is built from each target's stored changes, and the run options turn
into the same flags a user would type."""
import json
import sys

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
    assert {"override_vanilla_added_mid", "gui_vanilla_changed_mid", "loc_changed"} <= kinds
    assert isinstance(payload["info"], int) and payload["dismissed"] == 0
    for r in payload["records"]:
        assert r["id"] == r["fid"][:8]


def test_every_change_of_a_target_shares_its_block_view(cli):
    payload = _payload(cli, "--overrides")
    lines = _records(payload, "some_building")
    assert len(lines) == 2
    [bid] = {r["block"] for r in lines}
    block = payload["blocks"][bid]
    assert block["lines"][0] == "REPLACE:some_building = {" and block["line"] == 1
    assert sorted(c["fid"] for c in block["changes"]) == sorted(r["fid"] for r in lines)


def test_records_carry_the_yours_vanilla_pair(cli):
    lines = _records(_payload(cli, "--overrides"), "some_building")
    added = next(r for r in lines if r["kind"] == "override_vanilla_added_mid")
    assert (added["yours"], added["vanilla"]) == ("(missing)", "added upkeep = 5  (1.1.0)")
    assert added["since"] == "1.1.0" and added["base"] == "1.0.0"


def test_gui_findings_point_at_their_definitions_block(cli):
    payload = _payload(cli, "--gui")
    foo = next(r for r in _records(payload, "foo") if r["kind"] == "gui_vanilla_changed_mid")
    block = payload["blocks"][foo["block"]]
    assert block["lines"] == ["template foo = {", "\tsize = { 10 10 }", "}"]
    assert block["vanilla_file"] == "in_game/gui/vanilla.gui"
    [change] = block["changes"]
    assert (change["kind"], change["mark"], change["first"], change["last"], change["vanilla"]) == (
        "vanilla_changed", "review", 2, 2, "20 20")


def _change(**kw):
    base = {"kind": "vanilla_changed", "mark": "stale", "fid": "f" * 40, "since": "1.1",
            "first": None, "last": None, "cols": None, "anchor": None, "inside": False, "vanilla": None}
    base.update(kw)
    return base


def _rows(lines, *changes, line=10):
    rows = results.block_rows({"lines": lines, "line": line, "changes": list(changes)})
    return [(r["n"], r["text"], r["mark"], r["sign"]) for r in rows]


def test_a_pair_shows_vanillas_text_under_yours_with_the_words_that_differ():
    rows = results.block_rows({"line": 10, "lines": ["x = {", "\trequires = a", "}"], "changes": [
        _change(first=11, last=11, cols=[1, 13], vanilla="requires = b")]})
    assert [(r["n"], r["text"], r["sign"], r["ghost"]) for r in rows] == [
        (10, "x = {", None, False), (11, "\trequires = a", "-", False),
        (None, "\trequires = b", "+", True), (12, "}", None, False)]
    assert rows[1]["emph"] == [(12, 13)] and rows[2]["emph"] == [(12, 13)]


def test_a_value_inside_a_one_line_block_keeps_its_key_in_vanillas_row():
    rows = results.block_rows({"line": 10, "lines": ["icon = {", "\tsize = { 22 31 }", "}"], "changes": [
        _change(first=11, last=11, cols=[10, 15], vanilla="31 31")]})
    assert [(r["text"], r["sign"]) for r in rows[1:3]] == [("\tsize = { 22 31 }", "-"), ("\tsize = { 31 31 }", "+")]
    assert rows[1]["emph"] == [(10, 12)] and rows[2]["emph"] == [(10, 12)]


def test_vanillas_block_takes_the_copys_indentation():
    lines = ["t = {", "    box = {", "        a = 1", "    }", "}"]
    assert _rows(lines, _change(first=11, last=13, cols=[4, 5], vanilla="box = {\n\t\ta = 2\n\t\tb = 3\n}"))[4:8] == [
        (None, "    box = {", "stale", "+"), (None, "        a = 2", "stale", "+"),
        (None, "        b = 3", "stale", "+"), (None, "    }", "stale", "+")]


def test_a_statement_only_vanilla_has_goes_where_it_belongs():
    lines = ["t = {", "\ta = 1", "}"]
    assert _rows(lines, _change(kind="vanilla_added", mark="review", anchor=11, vanilla="n = 0")) == [
        (10, "t = {", None, None), (11, "\ta = 1", None, None),
        (None, "\tn = 0", "review", "+"), (12, "}", None, None)]
    assert _rows(lines, _change(kind="vanilla_added", anchor=10, inside=True, vanilla="n = 0"))[1] == (
        None, "\tn = 0", "stale", "+")
    assert _rows(lines, _change(kind="vanilla_added", anchor=9, vanilla="top = 0"))[0] == (
        None, "top = 0", "stale", "+")


def test_a_change_inside_a_shown_block_is_marked_but_not_shown_twice():
    lines = ["t = {", "\tbox = {", "\t\ta = 1", "\t}", "}"]
    rows = _rows(lines, _change(first=11, last=13, cols=[1, 2], vanilla="box = {\n\ta = 2\n}"),
                 _change(first=12, last=12, cols=[2, 7], vanilla="a = 2"))
    assert [r for r in rows if r[0] is None] == [
        (None, "\tbox = {", "stale", "+"), (None, "\t\ta = 2", "stale", "+"), (None, "\t}", "stale", "+")]


def test_info_changes_carry_no_mark():
    lines = ["t = {", "\ta = 9", "}"]
    assert _rows(lines, _change(kind="mod_changed", mark=None, fid=None, first=11, last=11,
                                cols=[1, 6], vanilla="a = 1")) == [
        (10, "t = {", None, None), (11, "\ta = 9", None, None), (12, "}", None, None)]


def test_spaces_indent_by_the_copys_own_step():
    lines = ["t = {", "  a = {", "    b = 1", "  }", "}"]
    assert _rows(lines, _change(kind="vanilla_added", anchor=11, inside=True, vanilla="c = 2"))[2] == (
        None, "    c = 2", "stale", "+")


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
    assert all(r["open"] is True for r in rows)
    assert all(r["open"] is False for r in results.source_rows(dupe, cli.world.mod, expanded=False))
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
