"""GUI names in the dependency audit: the types, templates and data-binding names a
mod's .gui files use that vanilla dropped, each with the patch that dropped it. The
cases come from the 1.4 port: vanilla removed the type header_action_button_left and
the data type ImportExportMarker, and kept plot_line, building_employment_piechart_data
and the font templates."""
import io
from contextlib import redirect_stdout

from conftest import build_tracker, make_ctx, _write_tree
from pdxaudit.gui_names import binding_names, file_names, run_deps, run_gui_names_audit

BOM = "﻿"
SCRIPT = {"in_game/common/buildings/b.txt": "a = 1\n"}

BUTTONS_OLD = ("types Buttons {\n"
               "\ttype header_action_button_left = button {\n\t\tsize = { 1 1 }\n\t}\n"
               "\ttype kept_button = button {\n\t}\n"
               "}\n")
BUTTONS_NEW = "types Buttons {\n\ttype kept_button = button {\n\t}\n}\n"
MARKERS_OLD = ('window = {\n'
               '\tdatacontext = "[ImportExportMarker.GetLocation]"\n'
               '\ttext = "[market|e]"\n'
               '\ttooltip = "[Moved.GetName]"\n'
               '}\n')
MARKERS_NEW = 'window = {\n\tdatacontext = "[PortMarker.GetLocation]"\n}\n'
SHARED = {
    "loading_screen/gui/preload/fonts.gui": "template Font_Size_Small {\n\tfontsize = 14\n}\n",
    "main_menu/gui/shared/building_tooltips.gui":
        BOM + "template building_employment_piechart_data {\n\tx = 1\n}\n",
}


def _tracker(tmp_path):
    old = {**SCRIPT, **SHARED,
           "in_game/gui/shared/buttons.gui": BUTTONS_OLD,
           "in_game/gui/shared/plotlines.gui": "template plot_line {\n\tx = 1\n}\n",
           "main_menu/gui/shared/old.gui": "template old_main_menu_template {\n\tx = 1\n}\n",
           # Pre-patch defined the fonts in main_menu too. The loading_screen copy stays.
           "main_menu/gui/preload/fonts.gui": "template Font_Size_Small {\n\tfontsize = 14\n}\n",
           "in_game/gui/map_markers.gui": MARKERS_OLD}
    new = {**SCRIPT, **SHARED,
           "in_game/gui/shared/buttons.gui": BUTTONS_NEW,
           # A BOM before the first definition must not hide it.
           "in_game/gui/shared/plotlines.gui": BOM + "template plot_line {\n\tx = 1\n}\n",
           "main_menu/gui/shared/old.gui": "template other {\n\tx = 1\n}\n",
           "in_game/gui/map_markers.gui": MARKERS_NEW,
           # Vanilla moved this binding name from a .gui file to localization.
           "main_menu/localization/english/m_l_english.yml": 'l_english:\n K:0 "[Moved.GetName]"\n'}
    return build_tracker(tmp_path, [("1.0", old), ("1.1", new)])


MOD_GUI = ('types Mine {\n'
           '\ttype my_button = header_action_button_left {\n\t}\n'
           '\ttype mod_type = button {\n\t}\n'
           '}\n'
           'window = {\n'
           '\theader_action_button_left = {\n\t\tusing = Font_Size_Small\n\t}\n'
           '\tmod_type = { using = plot_line }\n'
           '\twidget = { using = building_employment_piechart_data }\n'
           '\twidget = { using = old_main_menu_template }\n'
           '\tkept_button = {\n'
           '\t\tdatacontext = "[ImportExportMarker.GetLocation]"\n'
           '\t\ttext = "[market|e]"\n'
           '\t\ttooltip = "[Moved.GetName]"\n'
           '\t}\n'
           '\t# old_commented = { datacontext = "[Gone.Thing]" }\n'
           '}\n')


def _mod(tmp_path, gui=MOD_GUI):
    mod = tmp_path / "mod"
    _write_tree(mod, {".metadata/metadata.json": '{"id": "t"}', "in_game/gui/mine.gui": gui})
    return mod


def _run(tr, mod, fn=run_gui_names_audit, **kw):
    if fn is run_gui_names_audit:
        kw.setdefault("engine", False)
    buf = io.StringIO()
    with redirect_stdout(buf):
        findings = fn(mod, tr.repo, tr.hashes["1.0"], "1.0 Test", tr.hashes["1.1"], "1.1 Test",
                      make_ctx(tr.repo, "1.1"), **kw)
    return findings, buf.getvalue()


def test_removed_type_is_found_as_widget_key_and_type_parent(tmp_path):
    findings, out = _run(_tracker(tmp_path), _mod(tmp_path))
    f = [f for f in findings if f.name == "header_action_button_left"]
    assert len(f) == 1
    assert f[0].kind == "deps_gui_dropped" and f[0].detail == "type dropped in 1.1"
    assert f[0].since == "1.1" and f[0].location == "in_game/gui/mine.gui:2"
    assert f[0].key == {"target": "deps:gui/type/header_action_button_left", "use": "type"}
    assert "(+1 more)" in out


def test_removed_template_in_another_module_is_found(tmp_path):
    findings, _out = _run(_tracker(tmp_path), _mod(tmp_path))
    f = [f for f in findings if f.name == "old_main_menu_template"]
    assert [(x.kind, x.detail) for x in f] == [("deps_gui_dropped", "template dropped in 1.1")]


def test_removed_data_type_is_a_binding_finding(tmp_path):
    findings, out = _run(_tracker(tmp_path), _mod(tmp_path))
    f = [f for f in findings if f.kind == "deps_binding_dropped"]
    assert [(x.name, x.since) for x in f] == [("ImportExportMarker", "1.1")]
    assert f[0].detail.startswith("unused since 1.1")
    assert f[0].key == {"target": "deps:binding/ImportExportMarker", "use": "binding"}
    assert "used nowhere since 1.1" in out


def test_known_false_positives_are_not_reported(tmp_path):
    findings, _out = _run(_tracker(tmp_path), _mod(tmp_path))
    names = {f.name for f in findings}
    # still defined, behind a BOM or in another module
    assert not names & {"plot_line", "building_employment_piechart_data", "Font_Size_Small"}
    # the mod defines it, vanilla never defined it, or vanilla still defines it
    assert not names & {"mod_type", "my_button", "button", "widget", "window", "kept_button"}
    # still used, moved to localization, a game-concept link, or commented out
    assert not names & {"GetLocation", "Moved", "GetName", "market", "Gone", "Thing"}
    assert names == {"header_action_button_left", "old_main_menu_template", "ImportExportMarker"}


def test_mod_definition_of_a_removed_type_hides_the_finding(tmp_path):
    gui = MOD_GUI + "types Copy {\n\ttype header_action_button_left = button {\n\t}\n}\n"
    findings, _out = _run(_tracker(tmp_path), _mod(tmp_path, gui))
    assert "header_action_button_left" not in {f.name for f in findings}


def test_whole_audit_reports_a_template_once(tmp_path):
    """run_deps reports a `using` template one time."""
    gui = 'window = {\n\twidget = { using = plot_gone }\n}\n'
    lib_old = "template plot_gone {\n\tx = 1\n}\n"
    tr = build_tracker(tmp_path / "t2", [
        ("1.0", {**SCRIPT, "in_game/gui/lib.gui": lib_old}),
        ("1.1", {**SCRIPT, "in_game/gui/lib.gui": "template other {\n\tx = 1\n}\n"}),
    ])
    findings, _out = _run(tr, _mod(tmp_path, gui), fn=run_deps)
    assert [(f.kind, f.name) for f in findings] == [("deps_gui_dropped", "plot_gone")]


def test_binding_names():
    assert binding_names("[ImportExportMarker.GetPossibleItem.GetGoods]") == {
        "ImportExportMarker", "GetPossibleItem", "GetGoods"}
    assert binding_names("#high [Location.GetName|U]#! text") == {"Location", "GetName"}
    assert binding_names("[GetVariableSystem.Get('key_name')]") == {"GetVariableSystem", "Get"}
    assert binding_names("[Concept('c', '[Foo.Bar]')|E]") == {"Concept", "Foo", "Bar"}
    assert binding_names("[complacency|e] and $VALUE$ and @icon!") == set()
    assert binding_names("[Multiply_float('(float)2.5', Some.Value)]") == {"Multiply_float", "Some", "Value"}
    assert binding_names("LOC_KEY_ONLY") == set()


def test_file_names_reads_definitions_and_uses_by_structure():
    text = ('types T {\n\ttype a = b {\n\t}\n}\n'
            'local_template lt {\n}\n'
            'w = {\n\tusing = lt\n\tblockoverride "x" {\n\t\tc = { }\n\t}\n'
            '\ttext = [plain|e]\n\tvalue = "[X.Y]"\n}\n')
    found = file_names(text)
    assert found["defs"] == {("type", "a"), ("template", "lt")}
    assert set(found["uses"]) == {("type", "b", 2), ("template", "lt", 8), ("type", "w", 7),
                                  ("type", "c", 10), ("block", "x", 9)}
    assert sorted(found["bindings"]) == [("X", 13), ("Y", 13)]


def test_rename_candidate_is_measured_from_vanilla_sites(tmp_path):
    """Vanilla 1.1 writes PortMarker on the line where 1.0 had ImportExportMarker.
    The finding names the candidate and keeps it out of the id."""
    findings, out = _run(_tracker(tmp_path), _mod(tmp_path))
    f = next(f for f in findings if f.name == "ImportExportMarker")
    assert "vanilla uses PortMarker in its place at 1 of 1 sites" in f.detail
    assert f.data == {"rename_candidate": "PortMarker"}
    assert f.key == {"target": "deps:binding/ImportExportMarker", "use": "binding"}
    assert "Rename candidate" in out
    # header_action_button_left had no line that vanilla replaced with another type
    h = next(f for f in findings if f.name == "header_action_button_left")
    assert "in its place" not in h.detail


def test_engine_data_drops_known_names_and_confirms_the_rest(tmp_path):
    tr = _tracker(tmp_path)
    findings, out = _run(tr, _mod(tmp_path), engine={"ImportExportMarker", "GetLocation"})
    assert not [f for f in findings if f.kind.startswith("deps_binding")]
    assert "1 more that the engine still knows" in out
    findings, _out = _run(tr, _mod(tmp_path), engine={"GetLocation"})
    f = [f for f in findings if f.kind.startswith("deps_binding")]
    assert [(x.kind, x.name) for x in f] == [("deps_binding_removed", "ImportExportMarker")]


def test_engine_data_is_read_from_the_pdx_syntax_table(tmp_path):
    import sqlite3
    from pdxaudit.gui_names import engine_names
    db = tmp_path / "eu5_syntax.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE data_types (name TEXT)")
    con.executemany("INSERT INTO data_types VALUES (?)", [("EconomyView.GetEstimatedBalance",),
                                                         ("GetGlobalVariable",)])
    con.commit()
    con.close()
    assert engine_names(str(db)) == {"EconomyView", "GetEstimatedBalance", "GetGlobalVariable"}
    assert engine_names(str(tmp_path / "missing.db")) is None


def test_removed_block_is_found_and_mod_blocks_are_own(tmp_path):
    lib_old = 'types L {\n\ttype panel = widget {\n\t\tblock "caption" {}\n\t\tblock "kept" {}\n\t}\n}\n'
    lib_new = 'types L {\n\ttype panel = widget {\n\t\tblock "kept" {}\n\t}\n}\n'
    tr = build_tracker(tmp_path / "t3", [
        ("1.0", {**SCRIPT, "in_game/gui/lib.gui": lib_old}),
        ("1.1", {**SCRIPT, "in_game/gui/lib.gui": lib_new}),
    ])
    gui = ('window = {\n\tpanel = {\n\t\tblockoverride "caption" {}\n\t\tblockoverride "kept" {}\n'
           '\t\tblockoverride "mine" {}\n\t}\n\twidget = { block "mine" {} }\n}\n')
    findings, _out = _run(tr, _mod(tmp_path, gui))
    assert [(f.name, f.detail, f.location) for f in findings] == [
        ("caption", "block dropped in 1.1", "in_game/gui/mine.gui:3")]
