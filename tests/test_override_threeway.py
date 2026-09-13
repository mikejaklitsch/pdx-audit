"""Override audit end to end with three-way classification, per-block baselines
(adoption, recorded versions, open-finding bases) and single-value REPLACEs."""
import io
from contextlib import redirect_stdout

from conftest import build_tracker, make_ctx, audit_args, _write_tree
from pdxaudit.overrides import run_override_audit

B = "in_game/common/building_types/b.txt"
SV = "in_game/common/script_values/v.txt"


def _snap(block, value):
    return {B: block, SV: f"my_value = {value}\n"}


BLK_10 = "some_building = {\n\tcost = 100\n}\n"
BLK_11 = "some_building = {\n\tcost = 100\n\tupkeep = 5\n\textra = 1\n\tmore = 2\n}\n"
BLK_12 = "some_building = {\n\tcost = 200\n\tupkeep = 5\n\textra = 1\n\tmore = 2\n}\n"


def _tracker(tmp_path):
    return build_tracker(tmp_path, [("1.0", _snap(BLK_10, 5)), ("1.1", _snap(BLK_11, 5)),
                                    ("1.2", _snap(BLK_12, 7)), ("1.3", _snap(BLK_12, 7))])


def _mod(tmp_path):
    mod = tmp_path / "mod"
    _write_tree(mod, {
        ".metadata/metadata.json": '{"id": "t"}',
        "in_game/common/building_types/m.txt":
            "REPLACE:some_building = {\n\tcost = 100\n\tupkeep = 5\n\textra = 1\n\tmore = 2\n}\n",
        "in_game/common/script_values/m.txt": "REPLACE:my_value = 5\n",
    })
    return mod


def _run(tr, mod, old, new, fixed=False, bases=None):
    ctx = make_ctx(tr.repo, new, fixed=fixed, bases=bases)
    with redirect_stdout(io.StringIO()) as buf:
        findings = run_override_audit(mod, tr.repo, tr.hashes[old], f"{old} Test",
                                      tr.hashes[new], f"{new} Test", audit_args(), ctx)
    return findings, buf.getvalue()


def _find(findings, kind, name):
    return [f for f in findings if f.kind == kind and f.name == name]


def test_frozen_value_in_this_patch(tmp_path):
    tr = _tracker(tmp_path)
    findings, out = _run(tr, _mod(tmp_path), "1.1", "1.2")
    hit = _find(findings, "override_replace_frozen", "some_building")
    assert len(hit) == 1 and hit[0].since == "1.2"
    assert "cost" in hit[0].detail and "100" in hit[0].detail and "200" in hit[0].detail
    assert hit[0].key["target"] == "override:in_game/common/building_types/some_building"


def test_frozen_value_from_earlier_patch_carries_forward(tmp_path):
    tr = _tracker(tmp_path)
    findings, _ = _run(tr, _mod(tmp_path), "1.2", "1.3")
    hit = _find(findings, "override_replace_frozen", "some_building")
    assert len(hit) == 1 and hit[0].since == "1.2" and hit[0].base == "1.1"


def test_lines_already_adopted_are_informational(tmp_path):
    tr = _tracker(tmp_path)
    findings, _ = _run(tr, _mod(tmp_path), "1.0", "1.2", fixed=True)
    kinds = {f.kind for f in findings if f.name == "some_building"}
    assert "override_replace_merged" in kinds
    assert "override_replace_new_line" not in kinds
    assert "override_replace_frozen" in kinds


def test_scalar_replace_frozen(tmp_path):
    tr = _tracker(tmp_path)
    findings, _ = _run(tr, _mod(tmp_path), "1.1", "1.2")
    hit = _find(findings, "override_replace_frozen", "my_value")
    assert len(hit) == 1 and "5" in hit[0].detail and "7" in hit[0].detail
    assert not _find(findings, "override_nonblock", "my_value")


def test_scalar_carry_forward_through_open_finding_base(tmp_path):
    tr = _tracker(tmp_path)
    target = "override:in_game/common/script_values/my_value"
    findings, _ = _run(tr, _mod(tmp_path), "1.2", "1.3", bases={target: "1.1"})
    hit = _find(findings, "override_replace_frozen", "my_value")
    assert len(hit) == 1 and hit[0].since == "1.2"


def test_customized_value_changed_in_earlier_patch_stays_reported(tmp_path):
    blk = lambda g: f"navy_heavy_ship_build = {{\n\tgold = {g}\n}}\n"
    tr = build_tracker(tmp_path, [("1.0", {B: blk(50), SV: "my_value = 5\n"}),
                                  ("1.1", {B: blk(200), SV: "my_value = 5\n"}),
                                  ("1.2", {B: blk(200), SV: "my_value = 5\nother = 1\n"})])
    mod = tmp_path / "mod"
    _write_tree(mod, {".metadata/metadata.json": '{"id": "t"}',
                      "in_game/common/building_types/m.txt":
                          "REPLACE:navy_heavy_ship_build = {\n\tgold = 25\n}\n"})
    target = "override:in_game/common/building_types/navy_heavy_ship_build"
    # the patch that changed it reports it ...
    findings, _ = _run(tr, mod, "1.0", "1.1")
    hit = _find(findings, "override_replace_both_changed", "navy_heavy_ship_build")
    assert len(hit) == 1 and hit[0].since == "1.1" and hit[0].base == "1.0"
    assert "yours 25" in hit[0].detail and "50" in hit[0].detail and "200" in hit[0].detail
    # ... and the open finding's base keeps it reported after the window moves on
    findings, _ = _run(tr, mod, "1.1", "1.2", bases={target: "1.0"})
    hit = _find(findings, "override_replace_both_changed", "navy_heavy_ship_build")
    assert len(hit) == 1 and hit[0].since == "1.1" and hit[0].base == "1.0"


def test_replace_blocks_never_report_partial_adoption(tmp_path):
    tr = _tracker(tmp_path)
    for old, new in (("1.1", "1.2"), ("1.2", "1.3")):
        findings, _ = _run(tr, _mod(tmp_path), old, new)
        assert not [f for f in findings if "partial" in f.kind]


def test_detail_output_hides_dismissed_lines(tmp_path):
    from pdxaudit.ledger import finding_id, short_id
    tr = _tracker(tmp_path)
    findings, out = _run(tr, _mod(tmp_path), "1.1", "1.2")
    frozen = next(f for f in findings if f.kind == "override_replace_frozen"
                  and f.name == "some_building")
    fid = finding_id(frozen)
    assert f"[{short_id(fid)}]" in out
    ctx = make_ctx(tr.repo, "1.2", dismissed={fid})
    with redirect_stdout(io.StringIO()) as buf:
        run_override_audit(_mod(tmp_path), tr.repo, tr.hashes["1.1"], "1.1 Test",
                           tr.hashes["1.2"], "1.2 Test", audit_args(), ctx)
    hidden = buf.getvalue()
    assert f"[{short_id(fid)}]" not in hidden
    assert "1 dismissed" in hidden


def test_detail_output_names_classes(tmp_path):
    tr = _tracker(tmp_path)
    _findings, out = _run(tr, _mod(tmp_path), "1.1", "1.2")
    assert "some_building" in out
    lines = out.splitlines()
    i = next(n for n, l in enumerate(lines) if "✗ **kept at vanilla's old value**" in l)
    assert "cost" in lines[i + 1]
    assert lines[i + 2].strip() == "yours:    100"
    assert lines[i + 3].strip() == "vanilla:  100  →  200  (1.2)"
    # the detail shows the same id the summary does, so it can be dismissed from here
    from pdxaudit.ledger import finding_id, short_id
    frozen = next(f for f in _findings if f.kind == "override_replace_frozen")
    assert f"[{short_id(finding_id(frozen))}]" in lines[i + 1]
