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
