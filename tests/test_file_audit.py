"""The same-path file audit: a mod file at a vanilla file's path replaces the whole
file, so each definition in it is compared with vanilla's history, definitions
vanilla added after the copy was made are reported, and a file the tracker has no
history for is listed rather than skipped."""
import io
import types
from contextlib import redirect_stdout

from conftest import _write_tree, audit_args, build_tracker, make_ctx
from pdxaudit import files as file_audit
from pdxaudit.files import run_file_audit

EV = "in_game/events/flavor.txt"
LT = "in_game/map_data/location_templates.txt"
CSV = "in_game/map_data/adjacencies.csv"
META = {".metadata/metadata.json": '{"id": "t"}'}


def _run(tr, mod, old="1.0", new=None, args=None, tags=None):
    tags = tags or list(tr.hashes)
    new = new or tags[-1]
    with redirect_stdout(io.StringIO()) as buf:
        found = run_file_audit(mod, tr.repo, tr.hashes[old], f"{old} Test", tr.hashes[new], f"{new} Test",
                               args or audit_args(), make_ctx(tr.repo, new))
    return found, buf.getvalue()


def _kinds(found):
    return sorted((f.kind, f.name) for f in found)


def test_a_vanilla_change_inside_a_copied_definition_is_a_finding(tmp_path):
    v1 = "namespace = ev\nev.1 = {\n\toption = { a = 1 }\n}\n"
    v2 = "namespace = ev\nev.1 = {\n\toption = { a = 2 }\n}\n"
    tr = build_tracker(tmp_path, [("1.0", {EV: v1}), ("1.1", {EV: v2})])
    mod = tmp_path / "mod"
    _write_tree(mod, dict(META, **{EV: v1}))
    found, out = _run(tr, mod)
    [f] = found
    assert (f.kind, f.name, f.location, f.since, f.base) == (
        "file_vanilla_changed_mid", "ev.1", f"{EV}:3", "1.1", "1.0")
    assert f.key["yours"] == "a = 1" and f.key["vanilla"] == "a = 2"
    assert "File Copies Vanilla Changed (1 files)" in out


def test_a_definition_vanilla_added_after_the_copy_is_missing_from_the_game(tmp_path):
    v1 = "ev.1 = { a = 1 }\nev.2 = { b = 1 }\n"
    v2 = "ev.1 = { a = 1 }\nev.2 = { b = 1 }\nev.3 = { c = 1 }\n"
    tr = build_tracker(tmp_path, [("1.0", {EV: v1}), ("1.1", {EV: v2})])
    mod = tmp_path / "mod"
    # The copy leaves ev.2 out on purpose (it was there when the copy was made).
    _write_tree(mod, dict(META, **{EV: "ev.1 = { a = 1 }\n"}))
    found, out = _run(tr, mod)
    [f] = found
    assert (f.kind, f.name, f.since, f.base) == ("file_def_added", "ev.3", "1.1", "1.0")
    assert "vanilla added it in 1.1" in out


def test_a_definition_the_mod_moved_to_another_file_is_not_missing(tmp_path):
    v1 = "ev.1 = { a = 1 }\n"
    v2 = "ev.1 = { a = 1 }\nev.3 = { c = 1 }\n"
    tr = build_tracker(tmp_path, [("1.0", {EV: v1}), ("1.1", {EV: v2})])
    mod = tmp_path / "mod"
    _write_tree(mod, dict(META, **{EV: v1, "in_game/events/zz_moved.txt": "ev.3 = { c = 2 }\n"}))
    assert _run(tr, mod)[0] == []


def test_a_definition_vanilla_deleted_is_reported(tmp_path):
    v1 = "ev.1 = { a = 1 }\nev.2 = { b = 1 }\n"
    v2 = "ev.1 = { a = 1 }\n"
    tr = build_tracker(tmp_path, [("1.0", {EV: v1}), ("1.1", {EV: v2})])
    mod = tmp_path / "mod"
    _write_tree(mod, dict(META, **{EV: v1}))
    [f] = _run(tr, mod)[0]
    assert (f.kind, f.name, f.location, f.since) == ("file_def_removed", "ev.2", f"{EV}:2", "1.1")


def test_each_definition_has_its_own_baseline(tmp_path):
    # loc_b was brought up to 1.1; loc_a was not. Vanilla adds `x` to loc_a in 1.2,
    # after the version loc_a matches, so it is a change the copy lacks.
    v1 = "loc_a = { t = hills }\nloc_b = { t = flat }\n"
    v2 = "loc_a = { t = hills }\nloc_b = { t = forest }\n"
    v3 = "loc_a = { t = hills x = 1 }\nloc_b = { t = forest }\n"
    tr = build_tracker(tmp_path, [("1.0", {LT: v1}), ("1.1", {LT: v2}), ("1.2", {LT: v3})])
    mod = tmp_path / "mod"
    _write_tree(mod, dict(META, **{LT: "loc_a = { t = hills }\nloc_b = { t = forest }\n"}))
    [f] = _run(tr, mod)[0]
    assert (f.kind, f.name, f.since, f.base) == ("file_vanilla_added_mid", "loc_a", "1.2", "1.0")


def test_an_own_edit_is_not_a_finding(tmp_path):
    v1 = "loc_a = { t = hills }\n"
    v2 = "loc_a = { t = hills }\nloc_b = { t = flat }\n"
    tr = build_tracker(tmp_path, [("1.0", {LT: v1}), ("1.1", {LT: v2})])
    mod = tmp_path / "mod"
    _write_tree(mod, dict(META, **{LT: "loc_a = { t = mountains }\nloc_b = { t = flat }\n"}))
    assert _run(tr, mod)[0] == []


def test_a_repeated_top_level_key_is_one_definition_with_file_line_numbers(tmp_path):
    v1 = "locations = { a = 1 }\nother = { z = 1 }\nlocations = { b = 1 }\n"
    v2 = "locations = { a = 1 }\nother = { z = 1 }\nlocations = { b = 2 }\n"
    tr = build_tracker(tmp_path, [("1.0", {LT: v1}), ("1.1", {LT: v2})])
    mod = tmp_path / "mod"
    _write_tree(mod, dict(META, **{LT: v1}))
    [f] = _run(tr, mod)[0]
    assert (f.kind, f.name, f.location) == ("file_vanilla_changed_mid", "locations", f"{LT}:3")


def test_text_that_is_not_script_compares_line_by_line(tmp_path):
    v1 = "From;To;Type\na;b;sea\nc;d;sea\n"
    v2 = "From;To;Type\na;b;river\nc;d;sea\ne;f;sea\n"
    tr = build_tracker(tmp_path, [("1.0", {CSV: v1, "x.txt": ""}), ("1.1", {CSV: v2, "x.txt": ""})])
    mod = tmp_path / "mod"
    _write_tree(mod, dict(META, **{CSV: "From;To;Type\na;b;sea\nc;d;sea\ng;h;sea\n"}))
    found = sorted(_run(tr, mod)[0], key=lambda f: f.location)
    assert [(f.kind, f.location, f.key["yours"], f.key["vanilla"]) for f in found] == [
        ("file_vanilla_changed_mid", f"{CSV}:2", "a;b;sea", "a;b;river"),
        ("file_both_changed_mid", f"{CSV}:4", "g;h;sea", "e;f;sea"),
    ]


def test_a_format_older_commits_did_not_record_is_read_from_the_commits_that_did(tmp_path):
    # 1.0 recorded no .csv at all, so the file is not "added by vanilla" in 1.1.
    v2 = "a;b;sea\n"
    v3 = "a;b;river\n"
    tr = build_tracker(tmp_path, [("1.0", {"x.txt": ""}), ("1.1", {CSV: v2, "x.txt": ""}),
                                  ("1.2", {CSV: v3, "x.txt": ""})])
    mod = tmp_path / "mod"
    _write_tree(mod, dict(META, **{CSV: v2}))
    assert _kinds(_run(tr, mod)[0]) == [("file_vanilla_changed_mid", CSV)]


def test_a_file_vanilla_added_or_removed_is_a_review(tmp_path):
    other = "in_game/events/gone.txt"
    tr = build_tracker(tmp_path, [("1.0", {other: "ev.9 = { a = 1 }\n", "x.txt": ""}),
                                  ("1.1", {EV: "ev.1 = { a = 1 }\n", "x.txt": ""})])
    mod = tmp_path / "mod"
    _write_tree(mod, dict(META, **{EV: "ev.1 = { a = 1 }\n", other: "ev.9 = { a = 1 }\n"}))
    found = {(f.kind, f.name, f.detail, f.since) for f in _run(tr, mod)[0]}
    assert found == {("file_review", EV, "vanilla added", "1.1"),
                     ("file_review", other, "vanilla removed", "1.1")}


def test_a_file_without_vanilla_history_is_listed_not_skipped(tmp_path, monkeypatch):
    game = tmp_path / "game"
    _write_tree(game, {"in_game/map_data/default.map": "provinces = x\n"})
    (game / "main_menu/gfx").mkdir(parents=True)
    (game / "main_menu/gfx/icon.dds").write_bytes(b"DDS \0\0\x01")
    monkeypatch.setenv("PDX_GAME_ROOT", str(game))
    tr = build_tracker(tmp_path, [("1.0", {"x.txt": ""}), ("1.1", {"x.txt": ""})])
    mod = tmp_path / "mod"
    _write_tree(mod, dict(META, **{"in_game/map_data/default.map": "provinces = y\n"}))
    (mod / "main_menu/gfx").mkdir(parents=True)
    (mod / "main_menu/gfx/icon.dds").write_bytes(b"DDS \0\0\x01")
    found, out = _run(tr, mod)
    assert {(f.kind, f.name, f.detail) for f in found} == {
        ("file_untracked", "in_game/map_data/default.map", "differs from the installed game"),
        ("file_untracked", "main_menu/gfx/icon.dds", "identical to the installed game")}
    assert "Not Audited: No Vanilla History (2)" in out


def test_gui_and_localization_files_are_left_to_their_own_audits(tmp_path):
    gui, loc = "in_game/gui/a.gui", "main_menu/localization/english/a_l_english.yml"
    tr = build_tracker(tmp_path, [("1.0", {gui: "a = { x = 1 }\n", loc: "l_english:\n K:0 \"a\"\n"}),
                                  ("1.1", {gui: "a = { x = 2 }\n", loc: "l_english:\n K:0 \"b\"\n"})])
    mod = tmp_path / "mod"
    _write_tree(mod, dict(META, **{gui: "a = { x = 1 }\n", loc: "l_english:\n K:0 \"a\"\n"}))
    assert _run(tr, mod)[0] == []


def test_the_block_view_is_a_file_copy(tmp_path):
    v1 = "ev.1 = {\n\ta = 1\n}\n"
    v2 = "ev.1 = {\n\ta = 2\n}\n"
    tr = build_tracker(tmp_path, [("1.0", {EV: v1}), ("1.1", {EV: v2})])
    mod = tmp_path / "mod"
    _write_tree(mod, dict(META, **{EV: v1}))
    [f] = _run(tr, mod, args=types.SimpleNamespace(**vars(audit_args()), results_file="x.json"))[0]
    assert f.data["type"] == "File copy" and f.data["file"] == EV and f.data["line"] == 1
    assert [c["kind"] for c in f.data["changes"]] == ["vanilla_changed"]


def test_block_limits_the_audit_to_one_definition(tmp_path):
    v1 = "ev.1 = { a = 1 }\nev.2 = { b = 1 }\n"
    v2 = "ev.1 = { a = 2 }\nev.2 = { b = 2 }\nev.3 = { c = 1 }\n"
    tr = build_tracker(tmp_path, [("1.0", {EV: v1}), ("1.1", {EV: v2})])
    mod = tmp_path / "mod"
    _write_tree(mod, dict(META, **{EV: v1}))
    assert _kinds(_run(tr, mod, args=audit_args(block="ev.2"))[0]) == [("file_vanilla_changed_mid", "ev.2")]


def test_line_changes_keeps_a_hunk_the_copy_already_took():
    base, new = "a\nb\nc\n", "a\nB\nc\n"
    assert file_audit.line_changes("a\nB\nc\n", [base, new]) == (1, [])
    b, found = file_audit.line_changes("a\nb\nc\n", [base, new])
    assert b == 0 and [c["kind"] for c in found] == ["vanilla_changed"]


def test_the_split_respects_comments_strings_and_split_headers():
    text = ('namespace = ev\n'
            'scripted_trigger my_t = { always = yes }\n'
            'ev.1 = {  # a { in a comment\n'
            '\ttitle = "a } in a string"\n'
            '}\n'
            '@x = 5\n'
            'ev.2 =\n'
            '{\n'
            '\ta = 1\n'
            '}\n'
            'loc_a = { t = hills } loc_b = { t = flat }\n')
    defs = file_audit.definitions(text)
    assert list(defs) == ["(top level)", "scripted_trigger my_t", "ev.1", "ev.2", "loc_a"]
    assert defs["(top level)"].size == 2
    assert defs["ev.1"].body.startswith("ev.1 = {") and defs["ev.1"].body.endswith("}\n")
    assert defs["ev.2"].line == 7 and defs["ev.2"].size == 4
    assert defs["(top level)"].sig == "namespace = ev\n@x = 5"
    # comments and layout do not change what a definition reads
    same = file_audit.definitions(text.replace("# a { in a comment", "").replace("\ta = 1", "   a   =   1"))
    assert same["ev.1"].sig == defs["ev.1"].sig and same["ev.2"].sig == defs["ev.2"].sig


def test_a_very_large_definition_is_audited_entry_by_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(file_audit, "SPLIT_LINES", 3)
    v1 = "locations = {\n\ta = { rank = town }\n\tb = { rank = city }\n\tc = { rank = town }\n}\n"
    v2 = "locations = {\n\ta = { rank = city }\n\tb = { rank = city }\n\tc = { rank = town }\n\td = { rank = town }\n}\n"
    tr = build_tracker(tmp_path, [("1.0", {LT: v1}), ("1.1", {LT: v2})])
    mod = tmp_path / "mod"
    _write_tree(mod, dict(META, **{LT: v1}))
    found = _run(tr, mod)[0]
    assert sorted((f.kind, f.name, f.location) for f in found) == [
        ("file_def_added", "locations > d", LT),
        ("file_vanilla_changed_mid", "locations > a", f"{LT}:2")]
