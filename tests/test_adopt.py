"""Adopted sources: a unit the mod carries compared with the source's history, rename
rules, units upstream removed or added, whole files by path, localization keys, an
adopted source over a foundation, the adopted pass in a run, one group per target
compared with several bases, and records measured against a source."""
import io
import json
import sys
from contextlib import redirect_stderr, redirect_stdout

import pytest

import pdxaudit.config as config
from conftest import _write_tree, audit_args
from pdxaudit import adopt, ledger
from pdxaudit import sources as S
from pdxaudit.cli import main
from pdxaudit.report import Finding, render_triage
from test_sources import git_source, make_mod, meta

GUI = "in_game/gui"


@pytest.fixture
def env(world, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("PDX_GAME_ROOT", str(tmp_path / "no-game"))
    monkeypatch.setattr(config, "_CACHE", {})
    return world


def _run(env, mod, adopted, foundations=(), prior=(), **args):
    with redirect_stdout(io.StringIO()) as buf:
        findings, remedies = adopt.run_adopted_audit(mod, env.repo, adopted, list(foundations), audit_args(**args),
                                                     prior=prior)
    return findings, remedies, buf.getvalue()


def _adopt(env, tmp_path, commits, mod_files, mod_id="up", rename=None):
    up = git_source(tmp_path / mod_id, commits, mod_id=mod_id)
    mod = make_mod(tmp_path / "mod")
    _write_tree(mod, mod_files)
    S.add_source(mod, up, "adopted", vanilla_repo=env.repo)
    for frm, to in rename or ():
        S.add_rename(mod, mod_id, frm, to)
    return mod, S.load_sources(mod).by_id(mod_id)


def test_a_definition_upstream_changed_after_the_copy_is_a_change_measured_against_the_source(env, tmp_path):
    mod, src = _adopt(env, tmp_path, [
        ("1.0", {f"{GUI}/up.gui": "template w = {\n\tsize = 1\n}\n"}),
        ("1.1", {f"{GUI}/up.gui": "template w = {\n\tsize = 2\n}\n"})],
        {f"{GUI}/mine.gui": "template w = {\n\tsize = 1\n}\n"})
    findings, _rem, out = _run(env, mod, [src])
    f = next(f for f in findings if f.name == "w")
    assert f.kind == "gui_vanilla_changed_mid" and f.key["base"] == "up"
    assert f.since == "1.1" and f.base == "1.0"
    assert "**up** (git), 2 versions up to 1.1: 1 findings" in out


def test_rename_rules_match_renamed_names_but_never_vanilla_names(env, tmp_path):
    mod, src = _adopt(env, tmp_path, [
        ("1.0", {f"{GUI}/old_panel.gui": "template old_w = {\n\tsize = 1\n\tbar = old_thing\n}\n"}),
        ("1.1", {f"{GUI}/old_panel.gui": "template old_w = {\n\tsize = 2\n\tbar = old_thing\n}\n"})],
        {f"{GUI}/elsewhere.gui": "template new_w = {\n\tsize = 1\n\tbar = new_thing\n}\n"},
        rename=[("old_", "new_")])
    findings, _rem, _out = _run(env, mod, [src])
    assert [(f.name, f.kind) for f in findings] == [("new_w", "gui_vanilla_changed_mid")]
    text_of, path_of = adopt.renamer([{"from": "old_", "to": "new_"}], protected={"old_vanilla"})
    assert text_of("x = old_a y = old_vanilla z = bold_b") == "x = new_a y = old_vanilla z = bold_b"
    assert path_of("in_game/gui/old_panel.gui") == "in_game/gui/new_panel.gui"


def test_a_unit_upstream_removed_is_stale(env, tmp_path):
    mod, src = _adopt(env, tmp_path, [
        ("1.0", {f"{GUI}/up.gui": "template w = {\n}\ntemplate v = {\n}\n"}),
        ("1.1", {f"{GUI}/up.gui": "template v = {\n}\n"})],
        {f"{GUI}/mine.gui": "template w = {\n}\n"})
    findings, _rem, out = _run(env, mod, [src])
    assert [(f.kind, f.name, f.since) for f in findings] == [("adopted_unit_removed", "w", "1.1")]
    assert "up removed it in 1.1" in out


def test_a_vanilla_unit_the_source_stopped_overriding_is_vanilla_s_again(env, tmp_path):
    cat = "in_game/common/building_types"
    mod, src = _adopt(env, tmp_path, [
        ("1.0", {f"{cat}/up.txt": "REPLACE:some_building = {\n\tcost = 1\n}\nINJECT:other = {\n\tx = 1\n}\n"}),
        ("1.1", {f"{cat}/up.txt": "unrelated = {\n}\n"})],
        {f"{cat}/b.txt": "some_building = {\n\tcost = 100\n\tupkeep = 5\n}\n",
         f"{cat}/c.txt": "other = {\n\tx = 1\n}\n"})
    assert _run(env, mod, [src])[0] == []       # some_building is vanilla's; a plain `other` is no INJECT


def test_a_merging_types_entry_is_not_compared_with_a_definition(env, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_CACHE", {"merge_types": ["on_action"]})
    cat = "in_game/common/on_action"
    mod, src = _adopt(env, tmp_path, [
        ("1.0", {f"{cat}/up.txt": "on_start = {\n\ton_actions = { up_a }\n}\n"}),
        ("1.1", {f"{cat}/up.txt": "on_start = {\n\ton_actions = { up_b }\n}\n"})],
        {f"{cat}/mine.txt": "REPLACE:on_start = {\n\ton_actions = { up_a mine }\n}\n"})
    assert _run(env, mod, [src])[0] == []


def test_a_unit_upstream_added_to_a_file_the_mod_carries_returns_after_dismissal_when_it_changes(env, tmp_path):
    up = tmp_path / "up"
    mod, src = _adopt(env, tmp_path, [
        ("1.0", {f"{GUI}/up.gui": "template w = {\n}\n", f"{GUI}/other.gui": "template o = {\n}\n"}),
        ("1.1", {f"{GUI}/up.gui": "template w = {\n}\ntemplate added = {\n\ta = 1\n}\n",
                 f"{GUI}/other.gui": "template o = {\n}\ntemplate o2 = {\n}\n"})],
        {f"{GUI}/mine.gui": "template w = {\n}\n"})
    findings, _rem, _out = _run(env, mod, [src])
    added = [f for f in findings if f.kind == "adopted_unit_added"]
    assert [(f.name, f.location, f.since) for f in added] == [("added", f"{GUI}/up.gui", "1.1")]  # not o2
    first = ledger.finding_id(added[0])
    git_source(up, [("1.2", {f"{GUI}/up.gui": "template w = {\n}\ntemplate added = {\n\ta = 2\n}\n"})],
               mod_id="up")
    again = [f for f in _run(env, mod, [S.load_sources(mod).by_id("up")])[0] if f.kind == "adopted_unit_added"]
    assert ledger.finding_id(again[0]) != first


def test_a_whole_file_matches_by_path_and_its_definitions_go_with_it(env, tmp_path):
    mod, src = _adopt(env, tmp_path, [
        ("1.0", {f"{GUI}/same.gui": "template a = {\n\tx = 1\n}\n"}),
        ("1.1", {f"{GUI}/same.gui": "template a = {\n\tx = 2\n}\n"})],
        {f"{GUI}/same.gui": "template a = {\n\tx = 1\n}\n"})
    findings, _rem, _out = _run(env, mod, [src])
    assert [(f.name, f.key["target"]) for f in findings] == [(f"{GUI}/same.gui", f"guifile:{GUI}/same.gui")]


def test_localization_keys_and_script_definitions(env, tmp_path):
    loc = "main_menu/localization/english"
    cat = "in_game/common/scripted_effects"
    mod, src = _adopt(env, tmp_path, [
        ("1.0", {f"{loc}/up_l_english.yml": 'l_english:\n K:0 "one"\n', f"{cat}/up.txt": "fx = {\n\ta = 1\n}\n"}),
        ("1.1", {f"{loc}/up_l_english.yml": 'l_english:\n K:0 "two"\n', f"{cat}/up.txt": "fx = {\n\ta = 2\n}\n"})],
        {f"{loc}/mine_l_english.yml": 'l_english:\n K:0 "one"\n', f"{cat}/mine.txt": "fx = {\n\ta = 1\n}\n"})
    findings, _rem, _out = _run(env, mod, [src])
    kinds = {f.kind: f for f in findings}
    assert kinds["loc_changed"].key == {"target": "loc:english/K", "old": "one", "new": "two", "mod": "one",
                                        "base": "up"}
    assert kinds["override_vanilla_changed_mid"].key["target"] == f"script:{cat}/fx"


def test_files_the_mod_carries_nothing_from_are_not_reported(env, tmp_path):
    mod, src = _adopt(env, tmp_path, [
        ("1.0", {f"{GUI}/up.gui": "template x = {\n}\n"}),
        ("1.1", {f"{GUI}/up.gui": "template x = {\n}\ntemplate y = {\n}\n"})],
        {f"{GUI}/mine.gui": "template unrelated = {\n}\n"})
    assert _run(env, mod, [src])[0] == []


def test_an_adopted_source_over_a_foundation_is_compared_only_by_its_own_layer(env, tmp_path):
    frame = git_source(tmp_path / "frame", [("1.0", {f"{GUI}/frame.gui": "template f = {\n\tx = 1\n}\n"})],
                       mod_id="frame")
    up = tmp_path / "up"
    git_source(up, [("1.0", {f"{GUI}/up.gui": "template f = {\n\tx = 1\n}\ntemplate own = {\n\ty = 1\n}\n"}),
                    ("1.1", {f"{GUI}/up.gui": "template f = {\n\tx = 1\n}\ntemplate own = {\n\ty = 2\n}\n"})],
               mod_id="up")
    _write_tree(up, {".metadata/metadata.json": json.dumps({"id": "up", "version": "1.1", "relationships": [
        {"rel_type": "dependency", "id": "frame"}]})})
    mod = make_mod(tmp_path / "mod")
    _write_tree(mod, {f"{GUI}/mine.gui": "template f = {\n\tx = 1\n}\ntemplate own = {\n\ty = 1\n}\n"})
    S.add_source(mod, frame, "foundation", vanilla_repo=env.repo)
    S.add_source(mod, up, "adopted", vanilla_repo=env.repo)
    sources = S.load_sources(mod)
    report = adopt.layer_report(__import__("pdxaudit.base", fromlist=["StackBase"]).StackBase(
        env.repo, sources.foundations), sources.adopted[0], sources.adopted[0].raw_versions()[-1][0])
    assert report[("gui", ("in_game", "template", "f"))] == "same"
    assert report[("gui", ("in_game", "template", "own"))] == "new"
    findings, _rem, out = _run(env, mod, sources.adopted, sources.foundations)
    assert [f.name for f in findings] == ["own"]
    assert "units belong to its foundations" in out


def test_one_group_per_target_with_the_source_version_as_its_remedy(env, tmp_path):
    mod, src = _adopt(env, tmp_path, [
        ("1.0", {f"{GUI}/up.gui": "template foo = {\n\tsize = { 10 10 }\n}\n"}),
        ("1.1", {f"{GUI}/up.gui": "template foo = {\n\tsize = { 20 20 }\n\tmore = 1\n}\n"})],
        {f"{GUI}/mine.gui": "template foo = {\n\tsize = { 10 10 }\n}\n"})
    vanilla_finding = Finding("gui_vanilla_changed_mid", "foo", f"{GUI}/mine.gui:2", "foo > size", None,
                              {"target": "gui:in_game/template/foo", "path": ["foo"], "yours": "size = { 10 10 }",
                               "vanilla": "size = { 20 20 }"}, "1.1.0", "1.0.0")
    findings, remedies, _out = _run(env, mod, [src], prior=[vanilla_finding])
    assert remedies == {"gui:in_game/template/foo": "take up 1.1, which already has vanilla's change"}
    out = render_triage([vanilla_finding] + findings, "", "1.1.0 Test", ["gui", "adopted"], new_tag="1.1.0",
                        remedies=remedies)
    assert "## Compared with more than one base" in out
    assert "Fix: take up 1.1, which already has vanilla's change." in out
    assert out.count("`foo`") == 1 and "  - [" in out and "up: " in out and "vanilla: " in out


def test_open_records_keep_the_source_and_its_own_tags():
    state = ledger.empty_state()
    f = Finding("gui_vanilla_changed_mid", "c1", "a.gui:1", "", None,
                {"target": "gui:in_game/template/c1", "base": "found"}, "found 2.1", "found 2.0")
    v = Finding("loc_changed", "K", "a.yml", "english", None, {"target": "loc:english/K"}, "1.1.0", "1.0.0")
    u = Finding("loc_changed", "L", "a.yml", "english", None, {"target": "loc:english/L", "base": "found"},
                "found 2.1", "found 2.0")
    ledger.update_open(state, [f, v, u], "1.1.0")
    entry = state["open"][ledger.finding_id(f)]
    assert entry["source"] == "found" and entry["since"] == "2.1" and entry["base"] == "2.0"
    assert "source" not in state["open"][ledger.finding_id(v)]
    assert ledger.bases_from_state(state, {None: ["1.0.0", "1.1.0"], "found": ["2.0", "2.1"]}) == {
        ("found", "loc:english/L"): "2.0", (None, "loc:english/K"): "1.0.0"}
    assert ledger.bases_from_state(state, ["1.0.0", "1.1.0"]) == {"loc:english/K": "1.0.0"}


@pytest.fixture
def cli(env, tmp_path, monkeypatch):
    def run(*argv):
        monkeypatch.setattr(sys, "argv", ["pdx-audit", "--mod-root", str(env.mod), "--vanilla-repo", env.repo,
                                          "--color", "never", *argv])
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with redirect_stdout(out), redirect_stderr(err):
            try:
                main()
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 1
        return code, out.getvalue(), err.getvalue()
    return run


def test_the_adopted_pass_in_runs(env, tmp_path, cli):
    up = git_source(tmp_path / "up", [("1.0", {f"{GUI}/aaa_mod.gui": "template foo = {\n\tsize = { 10 10 }\n}\n"}),
                                      ("1.1", {f"{GUI}/aaa_mod.gui": "template foo = {\n\tsize = { 30 30 }\n}\n"})],
                    mod_id="up")
    code, plain, _err = cli()
    assert cli("--add-source", str(up), "--as", "adopted")[0] == 0
    code, out, err = cli()
    assert code == 0 and "# Adopted Source Audit" in out and "Ran override, dependency, GUI, localization, " \
                                                               "duplicate, adopted source." in out
    code, only, _err = cli("--adopted", "up")
    assert "Ran adopted source." in only and "# GUI Override Audit" not in only
    code, vanilla_only, _err = cli("--vanilla-only")
    assert vanilla_only == plain
    assert cli("--adopted", "nothing")[0] == 1
    assert "## Changes up Made After Your Copy" in out and "upstream:" in out


def test_runs_against_one_base_never_close_open_findings(env, tmp_path, cli):
    up = git_source(tmp_path / "up", [("1.0", {f"{GUI}/aaa_mod.gui": "template foo = {\n\tsize = { 10 10 }\n}\n"})],
                    mod_id="up")
    cli("--add-source", str(up), "--as", "adopted")
    cli()
    record = tmp_path / "xdg" / "pdx-audit" / "testmod" / "record.json"
    before = record.read_text()
    assert json.loads(before)["open"]
    cli("--adopted", "up")
    cli("--vanilla-only")
    assert record.read_text() == before
