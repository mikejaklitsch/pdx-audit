"""Dependency audit: flags names the mod uses that vanilla used at some tracked
version but no longer uses, with the patch that dropped them. It never suggests
renames, and names the mod defines itself at top level are not flagged."""
import io
from contextlib import redirect_stdout

from conftest import build_tracker, make_ctx, _write_tree
from pdxaudit.overrides import run_deps_audit

F = "in_game/common/buildings/b.txt"


def _snap(with_old_key, farm_name, with_my_thing=False):
    text = f"{farm_name} = {{\n\tcost = 5\n"
    if with_old_key:
        text += "\told_key = 1\n"
    text += "}\n"
    if with_my_thing:
        text += "my_thing = {\n\tx = 1\n}\n"
    return {F: text}


def _tracker(tmp_path):
    return build_tracker(tmp_path, [
        ("1.0", _snap(True, "building_farm", with_my_thing=True)),
        ("1.1", _snap(True, "building_farm")),
        ("1.2", _snap(False, "building_granary")),
        ("1.3", _snap(False, "building_granary")),
    ])


def _mod(tmp_path):
    mod = tmp_path / "mod"
    _write_tree(mod, {
        ".metadata/metadata.json": '{"id": "t"}',
        "in_game/common/rules/r.txt":
            "some_rule = {\n\told_key = 1\n\thas_building = building_farm\n}\n",
        "in_game/common/things/t.txt": "my_thing = {\n\tx = 2\n}\n",
    })
    return mod


def _run(tr, mod, old, new, fixed=False):
    ctx = make_ctx(tr.repo, new, fixed=fixed)
    buf = io.StringIO()
    with redirect_stdout(buf):
        findings = run_deps_audit(mod, tr.repo, tr.hashes[old], f"{old} Test",
                                  tr.hashes[new], f"{new} Test", ctx)
    return findings, buf.getvalue(), ctx


def test_names_dropped_in_an_earlier_patch_are_flagged_with_the_patch(tmp_path):
    tr = _tracker(tmp_path)
    findings, out, ctx = _run(tr, _mod(tmp_path), "1.2", "1.3")
    key = [f for f in findings if f.kind == "deps_key_dropped" and f.name == "old_key"]
    ref = [f for f in findings if f.kind == "deps_ref_dropped" and f.name == "building_farm"]
    assert len(key) == 1 and key[0].since == "1.2" and "dropped in 1.2" in key[0].detail
    assert len(ref) == 1 and ref[0].since == "1.2"
    assert ctx.scanned["deps"] > 0


def test_no_rename_suggestions(tmp_path):
    tr = _tracker(tmp_path)
    findings, out, _ = _run(tr, _mod(tmp_path), "1.2", "1.3")
    assert "building_granary" not in out
    assert "rename" not in out.lower()
    assert not any("maybe" in (f.detail or "") for f in findings)


def test_names_the_mod_defines_at_top_level_are_not_flagged(tmp_path):
    tr = _tracker(tmp_path)
    findings, _out, _ = _run(tr, _mod(tmp_path), "1.2", "1.3")
    assert not any(f.name == "my_thing" for f in findings)


def test_fixed_window_only_looks_inside_the_window(tmp_path):
    tr = _tracker(tmp_path)
    findings, _out, _ = _run(tr, _mod(tmp_path), "1.2", "1.3", fixed=True)
    assert not findings
    findings, _out, _ = _run(tr, _mod(tmp_path), "1.1", "1.3", fixed=True)
    assert {f.name for f in findings} == {"old_key", "building_farm"}


def test_fingerprint_keys(tmp_path):
    tr = _tracker(tmp_path)
    findings, _out, _ = _run(tr, _mod(tmp_path), "1.2", "1.3")
    f = next(f for f in findings if f.name == "old_key")
    assert f.key == {"target": "deps:old_key", "use": "key"}


def test_gui_templates_and_blocks_vanilla_dropped_are_found(tmp_path):
    lib = "in_game/gui/shared/lib.gui"
    old = 'template vanilla_button = {\n\tsize = { 1 1 }\n}\ntypes T {\n\ttype t = widget {\n\t\tblock "caption" {}\n\t}\n}\n'
    new = 'template other_button = {\n\tsize = { 1 1 }\n}\ntypes T {\n\ttype t = widget {\n\t}\n}\n'
    tr = build_tracker(tmp_path, [("1.0", {lib: old, F: "a = 1\n"}), ("1.1", {lib: new, F: "a = 1\n"})])
    mod = tmp_path / "mod"
    _write_tree(mod, {
        ".metadata/metadata.json": '{"id": "t"}',
        # vanilla_button and "caption" came from vanilla; my_button and "mine" are the mod's own
        "in_game/gui/mine.gui": ('template my_button = {\n\tsize = { 1 1 }\n}\n'
                                 'window = {\n\tbutton = { using = vanilla_button }\n'
                                 '\tbutton = { using = my_button }\n'
                                 '\tt = { blockoverride "caption" {} blockoverride "mine" {} }\n'
                                 '\twidget = { block "mine" {} }  # using = commented_out\n}\n'),
    })
    with redirect_stdout(io.StringIO()) as buf:
        found = run_deps_audit(mod, tr.repo, tr.hashes["1.0"], "1.0 Test", tr.hashes["1.1"], "1.1 Test",
                               make_ctx(tr.repo, "1.1"))
    gui = sorted((f.name, f.detail, f.location) for f in found if f.kind == "deps_gui_dropped")
    assert gui == [("caption", "block dropped in 1.1", "in_game/gui/mine.gui:7"),
                   ("vanilla_button", "template dropped in 1.1", "in_game/gui/mine.gui:5")]
    assert "GUI templates and blocks the mod uses that vanilla no longer defines" in buf.getvalue()
