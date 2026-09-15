"""Duplicate audit: one source of truth per definition. Needs only the newest
vanilla snapshot. Types vanilla itself merges across files are exempt from the
multiple-sources and plain-in-other-file checks."""
import io
from contextlib import redirect_stdout

import pdxaudit.config as config
from conftest import build_tracker, make_ctx, audit_args, _write_tree
from pdxaudit.dupes import run_dupes_audit

VANILLA = {
    "in_game/common/building_types/a.txt":
        "toll_castle = {\n\tcost = 1\n}\nparish = {\n\tcost = 1\n}\n",
    "in_game/common/building_types/b.txt": "mansion = {\n\tcost = 1\n}\n",
    "in_game/common/on_action/x.txt": "on_game_start = {\n\tevents = { a.1 }\n}\n",
    "in_game/common/on_action/y.txt": "on_game_start = {\n\tevents = { b.1 }\n}\n",
    "loading_screen/common/defines/00_defines.txt": "NGame = {\n\tA = 1\n\tB = 2\n}\n",
    # vanilla itself splits NGame across files, which makes defines a merging type
    "loading_screen/common/defines/00_graphics.txt": "NGame = {\n\tC = 3\n}\n",
    "in_game/common/diseases/plague.txt": "plague = {\n}\nother_disease = {\n}\n",
    "in_game/gui/vanilla.gui": "template vanilla_tip = {\n}\n",
}

MOD = {
    ".metadata/metadata.json": '{"id": "t"}',
    "in_game/common/building_types/epbm.txt":
        "INJECT:toll_castle = {\n\tx = 1\n}\nINJECT:mansion = {\n\tx = 1\n}\n",
    "in_game/common/building_types/mnt.txt":
        "INJECT:toll_castle = {\n\ty = 1\n}\nREPLACE:mansion = {\n\tcost = 2\n}\n",
    "in_game/common/building_types/new_stuff.txt": "parish = {\n\tx = 1\n}\n",
    "in_game/common/building_types/own.txt": "my_building = {\n}\n",
    "in_game/common/building_types/own2.txt": "my_building = {\n}\n",
    "in_game/common/on_action/mod.txt": "on_game_start = {\n\tevents = { c.1 }\n}\n",
    "loading_screen/common/defines/m1.txt": "NGame = {\n\tA = 5\n}\n",
    "loading_screen/common/defines/m2.txt": "NGame = {\n\tA = 6\n\tB = 7\n}\n",
    "in_game/common/diseases/plague.txt": "",
    "in_game/gui/one.gui": "template tip = {\n}\n",
    "in_game/gui/two.gui": "template tip = {\n}\n",
}


def _setup(tmp_path):
    tr = build_tracker(tmp_path, [("1.0", VANILLA)])
    mod = tmp_path / "mod"
    _write_tree(mod, MOD)
    return tr, mod


def _run(tr, mod, monkeypatch, **args):
    monkeypatch.setattr(config, "_CACHE", args.pop("config", {}))
    ctx = make_ctx(tr.repo, "1.0")
    with redirect_stdout(io.StringIO()) as buf:
        findings = run_dupes_audit(mod, tr.repo, tr.hashes["1.0"], "1.0 Test",
                                   audit_args(**args), ctx)
    return findings, buf.getvalue(), ctx


def _by_name(findings):
    out = {}
    for f in findings:
        out.setdefault(f.name, set()).add(f.kind)
    return out


def test_every_duplicate_class(tmp_path, monkeypatch):
    tr, mod = _setup(tmp_path)
    findings, out, ctx = _run(tr, mod, monkeypatch)
    names = _by_name(findings)
    assert names["toll_castle"] == {"dupes_multiple_sources"}
    assert names["mansion"] == {"dupes_multiple_sources"}
    assert names["my_building"] == {"dupes_multiple_sources"}
    assert names["parish"] == {"dupes_plain_other_file"}
    assert names["NGame.A"] == {"dupes_define_key"}
    assert names["tip"] == {"dupes_gui_definition"}
    assert names["in_game/common/diseases/plague.txt"] == {"dupes_file_override_drops"}
    assert "on_game_start" not in names          # vanilla merges on_action across files
    assert "NGame" not in names and "NGame.B" not in names
    assert ctx.scanned["dupes"] > 0


def test_detail_lists_every_location(tmp_path, monkeypatch):
    tr, mod = _setup(tmp_path)
    findings, out, _ = _run(tr, mod, monkeypatch)
    toll = next(f for f in findings if f.name == "toll_castle")
    assert "epbm.txt" in toll.detail and "mnt.txt" in toll.detail
    mansion = next(f for f in findings if f.name == "mansion")
    assert "INJECT" in mansion.detail and "REPLACE" in mansion.detail
    drop = next(f for f in findings if f.kind == "dupes_file_override_drops")
    assert "plague" in drop.detail and "other_disease" in drop.detail


def test_category_and_block_filters(tmp_path, monkeypatch):
    tr, mod = _setup(tmp_path)
    findings, _o, _c = _run(tr, mod, monkeypatch, category="building_types")
    assert {f.name for f in findings} == {"toll_castle", "mansion", "my_building", "parish"}
    findings, _o, _c = _run(tr, mod, monkeypatch, block="toll_castle")
    assert {f.name for f in findings} == {"toll_castle"}


def test_config_can_add_merge_types(tmp_path, monkeypatch):
    tr, mod = _setup(tmp_path)
    findings, _o, _c = _run(tr, mod, monkeypatch, config={"merge_types": ["building_types"]})
    assert not {"toll_castle", "mansion", "my_building", "parish"} & {f.name for f in findings}


def test_fingerprint_targets(tmp_path, monkeypatch):
    tr, mod = _setup(tmp_path)
    findings, _o, _c = _run(tr, mod, monkeypatch)
    toll = next(f for f in findings if f.name == "toll_castle")
    assert toll.key["target"] == "dupes:common/building_types/toll_castle"


LOC = "main_menu/localization/english"


def _run_files(tmp_path, monkeypatch, files, **args):
    tr = build_tracker(tmp_path, [("1.0", VANILLA)])
    mod = tmp_path / "mod2"
    _write_tree(mod, {".metadata/metadata.json": '{"id": "t"}', **files})
    findings, out, _ctx = _run(tr, mod, monkeypatch, **args)
    return findings, out


def _kinds(findings, prefix):
    return sorted((f.kind, f.name) for f in findings if f.kind.startswith(prefix))


def test_loc_key_repeated_inside_one_file(tmp_path, monkeypatch):
    findings, out = _run_files(tmp_path, monkeypatch, {
        f"{LOC}/a_l_english.yml": 'l_english:\n KEY:0 "one"\n OTHER:0 "x"\n KEY:0 "two"\n'})
    assert _kinds(findings, "dupes_loc") == [("dupes_loc_key", "KEY")]
    f = findings[0]
    assert f.detail == f"{LOC}/a_l_english.yml:2; {LOC}/a_l_english.yml:4"
    assert "Localization Keys Defined More Than Once" in out


def test_loc_key_in_two_normal_files_differing_and_identical(tmp_path, monkeypatch):
    findings, _out = _run_files(tmp_path, monkeypatch, {
        f"{LOC}/a_l_english.yml": 'l_english:\n DIFF:0 "one"\n SAME:0 "same"\n',
        f"{LOC}/b_l_english.yml": 'l_english:\n DIFF:0 "two"\n SAME:0 "same"\n'})
    assert _kinds(findings, "dupes_loc") == [("dupes_loc_key", "DIFF"), ("dupes_loc_key_same", "SAME")]


def test_loc_key_in_two_replace_files(tmp_path, monkeypatch):
    findings, _out = _run_files(tmp_path, monkeypatch, {
        f"{LOC}/replace/a_l_english.yml": 'l_english:\n KEY:0 "one"\n',
        f"{LOC}/replace/b_l_english.yml": 'l_english:\n KEY:0 "two"\n'})
    assert _kinds(findings, "dupes_loc") == [("dupes_loc_key", "KEY")]
    assert findings[0].key["target"] == "dupes:loc/english/replace/KEY"


def test_replace_copy_overriding_a_normal_copy_is_not_reported(tmp_path, monkeypatch):
    findings, _out = _run_files(tmp_path, monkeypatch, {
        f"{LOC}/a_l_english.yml": 'l_english:\n KEY:0 "normal"\n',
        f"{LOC}/replace/b_l_english.yml": 'l_english:\n KEY:0 "replaced"\n'})
    assert _kinds(findings, "dupes_loc") == []


def test_loc_keys_are_language_scoped(tmp_path, monkeypatch):
    findings, _out = _run_files(tmp_path, monkeypatch, {
        f"{LOC}/a_l_english.yml": 'l_english:\n KEY:0 "one"\n',
        "main_menu/localization/french/a_l_french.yml": 'l_french:\n KEY:0 "un"\n'})
    assert _kinds(findings, "dupes_loc") == []


def test_localization_outside_a_module_localization_folder_is_not_read(tmp_path, monkeypatch):
    findings, _out = _run_files(tmp_path, monkeypatch, {
        f"{LOC}/a_l_english.yml": 'l_english:\n KEY:0 "one"\n',
        "in_game/common/localization/english/b_l_english.yml": 'l_english:\n KEY:0 "two"\n'})
    assert _kinds(findings, "dupes_loc") == []


def test_on_action_block_setting_effect_or_trigger_twice(tmp_path, monkeypatch):
    findings, out = _run_files(tmp_path, monkeypatch, {
        "in_game/common/on_action/m.txt":
            "on_a = {\n\teffect = { x = 1 }\n\teffect = { y = 1 }\n}\n"
            "on_b = {\n\ttrigger = { x = 1 }\n\tevents = { e.1 }\n\ttrigger = { y = 1 }\n}\n"})
    assert _kinds(findings, "dupes_on_action") == [("dupes_on_action_syntax", "on_a"),
                                                   ("dupes_on_action_syntax", "on_b")]
    on_a = next(f for f in findings if f.name == "on_a")
    assert on_a.location == "in_game/common/on_action/m.txt:2" and on_a.detail == "`effect` at lines 2, 3"


def test_on_action_effect_or_trigger_in_two_files(tmp_path, monkeypatch):
    findings, _out = _run_files(tmp_path, monkeypatch, {
        "in_game/common/on_action/a.txt": "on_a = {\n\teffect = { x = 1 }\n}\non_b = {\n\ttrigger = { x = 1 }\n}\n",
        "in_game/common/on_action/b.txt": "on_a = {\n\teffect = { y = 1 }\n}\non_b = {\n\ttrigger = { y = 1 }\n}\n"})
    assert _kinds(findings, "dupes_on_action") == [("dupes_on_action_key", "on_a"),
                                                   ("dupes_on_action_key", "on_b")]
    on_a = next(f for f in findings if f.name == "on_a")
    assert "a.txt:2" in on_a.detail and "b.txt:2" in on_a.detail


def test_on_action_keys_that_combine_are_not_reported(tmp_path, monkeypatch):
    findings, _out = _run_files(tmp_path, monkeypatch, {
        "in_game/common/on_action/a.txt": "on_a = {\n\ton_actions = { on_x }\n\tevents = { e.1 }\n"
                                          "\teffect = { x = 1 }\n}\n",
        "in_game/common/on_action/b.txt": "on_a = {\n\ton_actions = { on_y }\n\tevents = { e.2 }\n}\n"})
    assert _kinds(findings, "dupes_on_action") == []
    assert not [f for f in findings if f.name == "on_a"]


def test_new_duplicate_kinds_cannot_be_dismissed():
    from pdxaudit.ledger import NOT_DISMISSIBLE
    assert {"dupes_loc_key", "dupes_loc_key_same", "dupes_on_action_syntax",
            "dupes_on_action_key"} <= NOT_DISMISSIBLE
