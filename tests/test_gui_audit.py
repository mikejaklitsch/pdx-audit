"""The GUI audit on diff3: each shadowed definition and each same-path file is
compared with vanilla's tracked versions, and every change it lacks or conflicts
with is one finding carrying where it is, when vanilla made it, and both sides."""
import io
from contextlib import redirect_stdout

from conftest import _write_tree, audit_args, build_tracker, make_ctx
from pdxaudit import ledger, results
from pdxaudit.gui import run_gui_audit


def _run(world, args=None, old=None, mod=None):
    with redirect_stdout(io.StringIO()) as buf:
        findings = run_gui_audit(mod or world.mod, world.repo, old or world.old, "1.0.0 Test",
                                 world.new, "1.1.0 Test", args or audit_args(),
                                 make_ctx(world.repo, "1.1.0"))
    return findings, buf.getvalue()


def test_a_vanilla_change_the_copy_lacks_is_one_finding(world):
    findings, out = _run(world)
    [f] = findings
    assert (f.kind, f.name, f.location, f.since, f.base) == (
        "gui_vanilla_changed_mid", "foo", "in_game/gui/aaa_mod.gui:2", "1.1.0", "1.0.0")
    # `template foo = {` is one header, named like `template foo {` (it once
    # parsed as a stray word `template` and a block `foo`)
    assert f.key == {"target": "gui:in_game/template/foo", "path": ["template foo", "size"],
                     "yours": "10 10", "vanilla": "20 20"}
    assert "yours:    10 10" in out and "vanilla:  10 10  →  20 20  (1.1.0)" in out


def test_a_definition_moved_out_of_a_replaced_file_is_compared_where_it_lives(tmp_path):
    gui = "in_game/gui/markers.gui"
    window = "window = {\n\tname = w\n}\n"
    foo, baz = "template foo {\n\tsize = { 10 10 }\n}\n", "template baz {\n\tx = 1\n}\n"
    tr = build_tracker(tmp_path, [("1.0", {gui: window + foo}), ("1.1", {gui: window + foo + baz})])
    copy = {".metadata/metadata.json": '{"id": "t"}', gui: window}

    def file_findings(mod):
        with redirect_stdout(io.StringIO()):
            found = run_gui_audit(mod, tr.repo, tr.hashes["1.0"], "1.0 Test", tr.hashes["1.1"], "1.1 Test",
                                  audit_args(), make_ctx(tr.repo, "1.1"))
        return [f.kind for f in found if f.location.startswith(gui)]

    moved, lone = tmp_path / "moved", tmp_path / "lone"
    _write_tree(moved, dict(copy, **{"in_game/gui/moved.gui": foo + baz}))
    _write_tree(lone, copy)
    assert file_findings(moved) == []
    assert file_findings(lone) == ["gui_vanilla_added_high"]


def test_a_change_meeting_your_edit_is_mid(world):
    (world.mod / "in_game/gui/aaa_mod.gui").write_text("template foo = {\n\tsize = { 30 30 }\n}\n")
    [f] = _run(world)[0]
    assert f.kind == "gui_both_changed_mid" and f.key["yours"] == "30 30"


def test_a_dismissal_holds_until_either_side_changes(world):
    [before] = _run(world)[0]
    (world.mod / "in_game/gui/aaa_mod.gui").write_text("template foo = {\n\tsize = {10 10}\n}\n")
    [layout] = _run(world)[0]
    assert ledger.finding_id(layout) == ledger.finding_id(before)
    (world.mod / "in_game/gui/aaa_mod.gui").write_text("template foo = {\n\tsize = { 10 11 }\n}\n")
    [edited] = _run(world)[0]
    assert ledger.finding_id(edited) != ledger.finding_id(before)


def test_a_same_path_file_is_compared_statement_by_statement(world):
    _write_tree(world.mod, {"in_game/gui/vanilla.gui":
                            "template foo = {\n\tsize = { 10 10 }\n}\ntemplate bar = {\n\tx = 1\n}\n"})
    findings, out = _run(world)
    by_target = {f.key["target"]: f for f in findings}
    f = by_target["guifile:in_game/gui/vanilla.gui"]
    assert (f.kind, f.name, f.location) == ("gui_vanilla_changed_mid", "in_game/gui/vanilla.gui",
                                            "in_game/gui/vanilla.gui:2")
    assert "Same-Path File Replacements Vanilla Changed (1)" in out


def test_the_window_starts_at_old_when_it_is_given(world):
    findings, out = _run(world, audit_args(old="1.1.0"), old=world.new)
    assert findings == [] and "All GUI overrides are current with vanilla" in out


def test_block_skips_whole_files(world):
    _write_tree(world.mod, {"in_game/gui/vanilla.gui": "template foo = {\n\tsize = { 10 10 }\n}\n"})
    findings, _out = _run(world, audit_args(block="foo"))
    assert not any(f.key["target"].startswith("guifile:") for f in findings)


def test_changes_inside_one_block_vanilla_changed_are_one_finding(tmp_path):
    old = ('template t = {\n\tbutton = {\n\t\tonpressed = "[A]"\n\t\ttip = {\n\t\t\told = yes\n\t\t}\n\t}\n'
           '\tother = {\n\t\tv = 1\n\t}\n}\n')
    new = ('template t = {\n\tbutton = {\n\t\tname = "b"\n\t\ttip = {\n\t\t\tnew = yes\n\t\t}\n\t}\n'
           '\tother = {\n\t\tv = 2\n\t}\n}\n')
    tr = build_tracker(tmp_path, [("1.0", {"in_game/gui/v.gui": old}), ("1.1", {"in_game/gui/v.gui": new})])
    mod = tmp_path / "mod"
    _write_tree(mod, {".metadata/metadata.json": '{"id": "t"}', "in_game/gui/mine.gui": old})
    with redirect_stdout(io.StringIO()):
        findings = run_gui_audit(mod, tr.repo, tr.hashes["1.0"], "1.0 Test", tr.hashes["1.1"], "1.1 Test",
                                 audit_args(results_file="r.json"), make_ctx(tr.repo, "1.1"))
    block, single = sorted(findings, key=lambda f: f.kind)
    assert (block.kind, block.detail, block.location) == ("gui_block_changed_mid", "4 changes", "in_game/gui/mine.gui:2")
    assert (single.kind, single.key["yours"], single.key["vanilla"]) == ("gui_vanilla_changed_mid", "v = 1", "v = 2")
    rows = results.block_rows(block.data)
    assert {r["fid"] for r in rows if r["mark"]} == {ledger.finding_id(block), ledger.finding_id(single)}


def test_collision_is_reported_only_in_the_patch_vanilla_adds_the_name(tmp_path):
    tip = "template tip = {\n\ta = 1\n}\n"
    gone = "template gone = {\n\tb = 1\n}\n"
    late = "template late = {\n\tc = 1\n}\n"
    tr = build_tracker(tmp_path, [
        ("1.0", {"in_game/gui/v.gui": gone}),
        ("1.1", {"in_game/gui/v.gui": gone + tip}),
        ("1.2", {"in_game/gui/v.gui": "template tip = {\n\ta = 2\n}\n" + late})])
    mod = tmp_path / "mod"
    _write_tree(mod, {".metadata/metadata.json": '{"id": "t"}', "in_game/gui/mine.gui": gone + tip + late})
    with redirect_stdout(io.StringIO()):
        findings = run_gui_audit(mod, tr.repo, tr.hashes["1.1"], "1.1 Test", tr.hashes["1.2"],
                                 "1.2 Test", audit_args(), make_ctx(tr.repo, "1.2"))
    assert sorted((f.kind, f.name, f.since) for f in findings) == [
        ("gui_new_collision", "late", "1.2"), ("gui_van_removed", "gone", "1.2"),
        ("gui_vanilla_changed_mid", "tip", "1.2")]


def test_every_finding_of_a_definition_shares_its_block(world):
    (world.mod / "in_game/gui/aaa_mod.gui").write_text(
        "template foo = {\n\tsize = { 10 10 }\n\tlegacy = 1\n}\n")
    tr_findings, _out = _run(world, audit_args(results_file="r.json"))
    payload = results.build_payload(tr_findings, new_tag="1.1.0")
    [bid] = {r["block"] for r in payload["records"]}
    rows = results.block_rows(payload["blocks"][bid])
    assert [(r["n"], r["text"], r["mark"], r["sign"]) for r in rows] == [
        (1, "template foo = {", None, None), (2, "\tsize = { 10 10 }", "review", "-"),
        (None, "\tsize = { 20 20 }", "review", "+"), (3, "\tlegacy = 1", None, None), (4, "}", None, None)]
    assert rows[1]["emph"] == [(10, 15)] and rows[2]["emph"] == [(10, 15)]


def test_gui_keywords_are_read_in_any_case():
    """Vanilla 1.4 hud_topbar.gui opens its group with `Types HUD_TopbarTypes`. The
    engine reads the keyword in any case, so the parser does too."""
    from pdxaudit.gui import parse_gui_defs
    text = "Types HUD_TopbarTypes\n{\n\tType topbar = widget {\n\t\tsize = { 1 1 }\n\t}\n}\nTemplate t1 {\n\tx = 1\n}\n"
    defs, clean = parse_gui_defs(text)
    assert clean
    assert [(d["kind"], d["name"]) for d in defs] == [("type", "topbar"), ("types", "HUD_TopbarTypes"), ("template", "t1")]
