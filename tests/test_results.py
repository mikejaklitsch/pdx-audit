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


def test_side_by_side_compares_each_side_with_the_version_the_copy_matches():
    block = {"line": 10, "lines": ["REPLACE:x = {", "\tprice = 4 # mine", "\textra = 1", "}"],
             "vanilla_lines": ["x = {", "\tprice = 6", "}"], "base_lines": ["x = {", "\tprice = 8", "}"],
             "changes": [_change(kind="both_changed", mark="review", first=11, last=11, vfirst=2, vlast=2)]}
    rows = results.side_rows(block)
    side = lambda s: (s["state"], s["text"].strip()) if s else None
    assert [(side(r["left"]), side(r["right"]), r["mark"]) for r in rows] == [
        (("same", "x = {"), ("same", "REPLACE:x = {"), None),
        (("del", "price = 8"), ("del", "price = 8"), None),
        (("add", "price = 6"), ("add", "price = 4 # mine"), "review"),
        (None, ("add", "extra = 1"), None),
        (("same", "}"), ("same", "}"), None)]
    assert rows[2]["left"]["emph"] and rows[1]["left"]["emph"]     # 8 against 6, where one line replaced one
    assert [r["lead"] for r in rows] == [False, False, True, False, False]


def test_a_removed_block_takes_its_own_closing_brace():
    base = ["t {", "\ta = {", "\t\tv = 1", "\t\tb = {", "\t\t}", "\t}", "\tc = {", "\t\tv = 1", "\t}", "}"]
    mine = base[:6] + base[9:]
    block = {"line": 1, "lines": mine, "vanilla_lines": base, "base_lines": base, "changes": []}
    assert [r["right"]["text"] for r in results.side_rows(block) if r["right"]["state"] == "del"] == [
        "\tc = {", "\t\tv = 1", "\t}"]


def test_a_removed_sibling_block_takes_its_own_closing_brace():
    base = ["t {", "\ta = {", "\t\tb = {", "\t\t}", "\t}", "\tc = {", "\t\tv = 1", "\t}", "",
            "\td = {", "\t\tw = 1", "\t\tx = 2", "\t}", "}"]
    mine = base[:5] + base[8:]
    block = {"line": 1, "lines": mine, "vanilla_lines": base, "base_lines": base, "changes": []}
    rows = results.side_rows(block)
    assert [r["right"]["text"] for r in rows if r["right"]["state"] == "del"] == ["\tc = {", "\t\tv = 1", "\t}"]
    assert [r["right"]["n"] for r in rows if r["right"]["state"] == "same"] == list(range(1, 12))


def test_a_finding_takes_its_strongest_mark_and_the_blank_rows_inside_it():
    base = ["x = {", "\ta = 1", "}"]
    block = {"line": 1, "lines": ["x = {", "", "\ta = 2", "}"], "vanilla_lines": base, "base_lines": base,
             "changes": [_change(kind="both_changed", mark="review", vfirst=2, vlast=2),
                         _change(kind="both_changed", mark="stale", first=3, last=3)]}
    rows = results.side_rows(block)
    assert [(r["mark"], r["lead"]) for r in rows] == [
        (None, False), ("stale", True), ("stale", False), ("stale", False), (None, False)]
    assert rows[2]["right"]["quiet"] and rows[2]["fid"] == rows[1]["fid"]


def test_flattening_strips_indentation_and_joins_runs_of_closing_braces():
    rows = results.block_rows({"line": 1, "lines": ["a = {", "\tb = {", "\t\tc = 1", "\t}", "}"], "changes": []})
    assert [(r["n"], r["text"]) for r in results.flatten_rows(rows)] == [
        (1, "a = {"), (2, "b = {"), (3, "c = 1"), (4, "} }")]
    nested = ["a = {", "\tb = {", "\t\tc = 1", "\t}", "}"]
    block = {"line": 1, "lines": nested[:2] + ["\t\tc = 2"] + nested[3:], "vanilla_lines": nested,
             "base_lines": nested, "changes": []}
    flat = results.flatten_rows(results.side_rows(block))
    assert [((r["left"] or {}).get("text"), (r["right"] or {}).get("text")) for r in flat] == [
        ("a = {", "a = {"), ("b = {", "b = {"), ("c = 1", "c = 1"), (None, "c = 2"), ("} }", "} }")]


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


def test_an_inject_pairs_your_statements_with_vanillas_side_by_side():
    # An INJECT adds to vanilla's block, so neither side deleted anything: every line is
    # current text, and only the keys the INJECT writes are shown. Vanilla writing the
    # key over three lines against your one leaves the extra lines alone on its side.
    block = {"type": "INJECT", "file": "m.txt", "line": 139, "lines": [],
             "pairs": [{"key": "possible_production_methods", "fid": "f" * 40, "mark": "stale",
                        "since": 1,
                        "yours": [(140, "\tpossible_production_methods = { mine }")],
                        "vanilla": [(32, "\t\tpossible_production_methods ="),
                                    (33, "\t\t\testate_building_input"), (34, "\t\t}")]}]}
    rows = results.inject_side_rows(block)
    assert len(rows) == 3
    assert rows[0]["left"]["n"] == 32 and rows[0]["right"]["n"] == 140
    assert rows[0]["mark"] == "stale" and rows[0]["lead"] and rows[0]["cause"] == "inject"
    assert [r["right"] for r in rows[1:]] == [None, None]     # yours has no more lines
    assert {r["left"]["state"] for r in rows} == {"same"}     # nothing added or deleted
    assert not rows[1]["lead"]


def test_an_inject_pair_of_one_line_each_marks_the_words_that_differ():
    block = {"type": "INJECT", "file": "m.txt", "line": 5, "lines": [],
             "pairs": [{"key": "max_levels", "fid": "a" * 40, "mark": "stale", "since": 1,
                        "yours": [(5, "\tmax_levels = beer_guild_max_level")],
                        "vanilla": [(9, "\tmax_levels = 4")]}]}
    [row] = results.inject_side_rows(block)
    assert row["left"]["emph"] and row["right"]["emph"]


def test_a_finding_keeps_one_run_across_a_line_only_you_changed():
    # Your own deletion inside the block the finding covers must not break the finding
    # into two runs, since the view draws one outline around each run.
    base = ["x = {", "\ta = 1", "\tdrop = 1", "\tb = 1", "}"]
    block = {"line": 1, "lines": ["x = {", "\ta = 2", "\tb = 2", "}"],
             "vanilla_lines": ["x = {", "\ta = 9", "\tdrop = 1", "\tb = 9", "}"], "base_lines": base,
             "changes": [_change(kind="both_changed", mark="review", vfirst=2, vlast=2, first=2, last=2),
                         _change(kind="both_changed", mark="review", vfirst=4, vlast=4, first=3, last=3)]}
    rows = results.side_rows(block)
    assert sum(1 for r in rows if r["lead"]) == 1        # one outline, not two
    # `drop = 1`, which only your copy deleted, sits inside the run and not between two.
    assert [r["fid"] is not None for r in rows] == [False, False, True, True, True, True, False]
    assert rows[3]["left"]["text"].strip() == "drop = 1" and rows[3]["right"]["state"] == "del"


def _gapped_block(gap):
    """A block with two changes of one finding, `gap` unchanged lines apart."""
    filler = [f"\tk{i} = 1" for i in range(gap)]
    base = ["x = {", "\ta = 1", *filler, "\tb = 1", "}"]
    vanilla = ["x = {", "\ta = 9", *filler, "\tb = 9", "}"]
    mine = ["x = {", "\ta = 2", *filler, "\tb = 2", "}"]
    last = 3 + gap
    return {"line": 1, "lines": mine, "vanilla_lines": vanilla, "base_lines": base,
            "changes": [_change(kind="both_changed", mark="review", vfirst=2, vlast=2, first=2, last=2),
                        _change(kind="both_changed", mark="review", vfirst=last, vlast=last,
                                first=last, last=last)]}


def test_a_finding_splits_exactly_where_the_view_folds():
    # One function, results.hidden_span, decides both whether the view folds a stretch of
    # unmarked rows and whether the finding around it stays in one run. This checks that
    # the two never disagree: a finding breaks only where the view hides something.
    seen = set()
    for gap in range(1, 20):
        rows = results.side_rows(_gapped_block(gap))
        marked = [n for n, r in enumerate(rows) if r["fid"]]
        one_run = all(rows[n]["fid"] for n in range(marked[0], marked[-1] + 1))
        shows_all = not any("fold" in r for r in results.fold_rows(rows))
        assert one_run == shows_all, f"gap of {gap}: one_run={one_run} shows_all={shows_all}"
        seen.add(one_run)
    assert seen == {True, False}, "the gaps tried must cover both a joined and a folded one"
