"""The override audit on diff3: each REPLACE block is compared with vanilla's
tracked versions of it, single-value REPLACEs likewise, and INJECT targets keep
their injection-point check."""
import io
from contextlib import redirect_stdout

import pytest

from conftest import _write_tree, audit_args, build_tracker, make_ctx
from pdxaudit import results
from pdxaudit.overrides import run_override_audit

# These tests read one change's finding; the block mode has its own tests.
pytestmark = pytest.mark.usefixtures("statement_findings")

TARGET = "override:in_game/common/building_types/some_building"


def _run(world, args=None, old=None, mod=None):
    with redirect_stdout(io.StringIO()) as buf:
        findings = run_override_audit(mod or world.mod, world.repo, old or world.old, "1.0.0 Test",
                                      world.new, "1.1.0 Test", args or audit_args(),
                                      make_ctx(world.repo, "1.1.0"))
    return findings, buf.getvalue()


def test_each_vanilla_change_the_replace_lacks_is_one_finding(world):
    findings, out = _run(world)
    assert sorted((f.kind, f.location, f.key["yours"], f.key["vanilla"]) for f in findings) == [
        ("override_vanilla_added_mid", "in_game/common/building_types/m.txt:2", None, "upkeep = 5"),
        ("override_vanilla_removed_mid", "in_game/common/building_types/m.txt:3", "legacy_mod = 1", None)]
    assert all(f.key["target"] == TARGET and f.since == "1.1.0" and f.base == "1.0.0" for f in findings)
    assert "vanilla:  added upkeep = 5  (1.1.0)" in out and "2 REPLACE changes to accept or check" in out


def test_your_edit_raises_vanilla_changes_in_its_block_and_is_not_a_finding(world):
    (world.mod / "in_game/common/building_types/m.txt").write_text(
        "REPLACE:some_building = {\n\tcost = 150\n\tlegacy_mod = 1\n}\n")
    findings, out = _run(world)
    assert sorted(f.kind for f in findings) == ["override_vanilla_added_high", "override_vanilla_removed_high"]
    assert out.index("high priority") < out.index("legacy_mod")


def test_replace_or_create_is_compared_like_replace(world):
    path = world.mod / "in_game/common/building_types/m.txt"
    path.write_text(path.read_text().replace("REPLACE:", "REPLACE_OR_CREATE:"))
    findings, _out = _run(world)
    assert sorted(f.kind for f in findings) == ["override_vanilla_added_mid", "override_vanilla_removed_mid"]


def test_a_missing_target_is_expected_for_try_and_or_create(world):
    _write_tree(world.mod, {"in_game/common/building_types/n.txt":
                            "REPLACE_OR_CREATE:brand_new = {\n\tcost = 1\n}\n"
                            "INJECT_OR_CREATE:other_new = {\n\tcost = 2\n}\n"
                            "TRY_REPLACE:maybe_new = {\n\tcost = 3\n}\n"})
    findings, out = _run(world)
    assert not any(f.name in ("brand_new", "other_new", "maybe_new") for f in findings)
    assert "REPLACE_OR_CREATE:brand_new at `in_game/common/building_types/n.txt:1`: creates it" in out
    assert "TRY_REPLACE:maybe_new at `in_game/common/building_types/n.txt:7`: ignored" in out


def test_or_create_whose_target_vanilla_removed_now_creates_it(tmp_path):
    bt = "in_game/common/building_types/b.txt"
    tr = build_tracker(tmp_path, [("1.0", {bt: "gone_building = {\n\tcost = 1\n}\n"}),
                                  ("1.1", {bt: "other_building = {\n\tcost = 1\n}\n"})])
    mod = tmp_path / "mod"
    _write_tree(mod, {".metadata/metadata.json": '{"id": "t"}',
                      "in_game/common/building_types/m.txt": "REPLACE_OR_CREATE:gone_building = {\n\tcost = 5\n}\n"})
    with redirect_stdout(io.StringIO()):
        findings = run_override_audit(mod, tr.repo, tr.hashes["1.0"], "1.0 Test", tr.hashes["1.1"], "1.1 Test",
                                      audit_args(), make_ctx(tr.repo, "1.1"))
    assert [(f.kind, f.since) for f in findings] == [("override_now_created", "1.1")]


def test_the_window_starts_at_old_when_it_is_given(world):
    findings, out = _run(world, audit_args(old="1.1.0"), old=world.new)
    assert findings == [] and "All overrides are current with vanilla" in out


def test_single_value_replace_compares_the_statement(tmp_path):
    sv = "in_game/common/script_values/v.txt"
    tr = build_tracker(tmp_path, [("1.0", {sv: "my_value = 5\n"}), ("1.1", {sv: "my_value = 7\n"})])
    mod = tmp_path / "mod"
    _write_tree(mod, {".metadata/metadata.json": '{"id": "t"}',
                      "in_game/common/script_values/m.txt": "REPLACE:my_value = 5\n"})
    with redirect_stdout(io.StringIO()):
        [f] = run_override_audit(mod, tr.repo, tr.hashes["1.0"], "1.0 Test", tr.hashes["1.1"], "1.1 Test",
                                 audit_args(results_file="r.json"), make_ctx(tr.repo, "1.1"))
    assert (f.kind, f.key["yours"], f.key["vanilla"], f.since) == (
        "override_vanilla_changed_mid", "my_value = 5", "my_value = 7", "1.1")
    rows = results.block_rows(f.data)
    assert [(r["text"], r["sign"]) for r in rows] == [
        ("REPLACE:my_value = 5", "-"), ("REPLACE:my_value = 7", "+")]
    assert rows[0]["emph"] == [(19, 20)] and rows[1]["emph"] == [(19, 20)]


def test_an_orphaned_replace_names_the_patch_that_removed_its_block(tmp_path):
    b = "in_game/common/building_types/b.txt"
    tr = build_tracker(tmp_path, [("1.0", {b: "old_building = {\n\tcost = 1\n}\n"}),
                                  ("1.1", {b: "other = {\n\tcost = 1\n}\n"})])
    mod = tmp_path / "mod"
    _write_tree(mod, {".metadata/metadata.json": '{"id": "t"}',
                      "in_game/common/building_types/m.txt": "REPLACE:old_building = {\n\tcost = 2\n}\n"})
    with redirect_stdout(io.StringIO()):
        [f] = run_override_audit(mod, tr.repo, tr.hashes["1.0"], "1.0 Test", tr.hashes["1.1"], "1.1 Test",
                                 audit_args(), make_ctx(tr.repo, "1.1"))
    assert (f.kind, f.since) == ("override_orphaned", "1.1")


def test_the_side_by_side_view_lines_up_vanilla_and_the_replace(world):
    findings, _out = _run(world, audit_args(results_file="r.json"))
    rows = results.side_rows(findings[0].data)
    side = lambda s: (s["state"], s["text"]) if s else None
    assert [(side(r["left"]), side(r["right"]), r["mark"]) for r in rows] == [
        (("same", "some_building = {"), ("same", "REPLACE:some_building = {"), None),
        (("same", "\tcost = 100"), ("same", "\tcost = 100"), None),
        (("del", "\tlegacy_mod = 1"), ("same", "\tlegacy_mod = 1"), "review"),
        (("add", "\tupkeep = 5"), None, "review"),
        (("same", "}"), ("same", "}"), None)]


def test_an_inject_measured_from_a_carried_base_records_that_base(tmp_path):
    b = "in_game/common/building_types/b.txt"
    tr = build_tracker(tmp_path, [("1.0", {b: "thing = {\n\tcost = 1\n\tupkeep = 1\n}\n"}),
                                  ("1.1", {b: "thing = {\n\tcost = 2\n\tupkeep = 2\n}\n"}),
                                  ("1.2", {b: "thing = {\n\tcost = 2\n\tupkeep = 2\n\tx = 1\n}\n"})])
    mod = tmp_path / "mod"
    _write_tree(mod, {".metadata/metadata.json": '{"id": "t"}',
                      "in_game/common/building_types/i.txt": "INJECT:thing = {\n\tupkeep = 9\n}\n"})
    target = "override:in_game/common/building_types/thing"
    with redirect_stdout(io.StringIO()):
        findings = run_override_audit(mod, tr.repo, tr.hashes["1.1"], "1.1 Test", tr.hashes["1.2"], "1.2 Test",
                                      audit_args(), make_ctx(tr.repo, "1.2", bases={target: "1.0"}))
    [f] = [f for f in findings if f.kind == "override_inject_overlap"]
    assert (f.since, f.base) == ("1.1", "1.0")


def test_an_inject_colliding_with_vanilla_marks_its_key_in_the_block(world):
    _write_tree(world.mod, {"in_game/common/building_types/i.txt":
                            "INJECT:some_building = {\n\tupkeep = 7\n}\n"})
    findings, _out = _run(world, audit_args(results_file="r.json"))
    [inject] = [f for f in findings if f.kind == "override_inject_overlap"]
    rows = results.block_rows(inject.data)
    assert [(r["text"], r["mark"], r["sign"]) for r in rows] == [
        ("INJECT:some_building = {", None, None), ("\tupkeep = 7", "stale", "-"),
        ("\tupkeep = 5", "stale", "+"), ("}", None, None)]


def test_every_finding_of_a_block_points_at_one_stored_block(world):
    findings, _out = _run(world, audit_args(results_file="r.json"))
    assert len({id(f.data) for f in findings}) == 1
    payload = results.build_payload(findings, new_tag="1.1.0")
    assert len(payload["blocks"]) == 1 and len({r["block"] for r in payload["records"]}) == 1
    rows = results.block_rows(next(iter(payload["blocks"].values())))
    assert [(r["n"], r["text"], r["mark"], r["sign"]) for r in rows] == [
        (1, "REPLACE:some_building = {", None, None), (2, "\tcost = 100", None, None),
        (None, "\tupkeep = 5", "review", "+"), (3, "\tlegacy_mod = 1", "review", "-"), (4, "}", None, None)]


def test_a_dismissed_change_keeps_no_mark_in_the_block(world):
    findings, _out = _run(world, audit_args(results_file="r.json"))
    visible = [f for f in findings if f.kind != "override_vanilla_added_mid"]
    block = next(iter(results.build_payload(visible, new_tag="1.1.0")["blocks"].values()))
    rows = results.block_rows(block)
    assert not any(r["ghost"] for r in rows)
    assert [r["n"] for r in rows if r["mark"]] == [3]


def test_an_override_of_a_target_only_in_a_replaced_file_is_orphaned(tmp_path):
    a = "in_game/common/building_types/a.txt"
    vanilla = {a: "foo = {\n\tcost = 1\n}\nbar = {\n\tcost = 1\n}\nbaz = {\n\tcost = 1\n}\n"}
    tr = build_tracker(tmp_path, [("1.0", vanilla), ("1.1", vanilla)])
    mod = tmp_path / "mod"
    _write_tree(mod, {
        ".metadata/metadata.json": '{"id": "t"}',
        # The copy of a.txt drops foo, keeps baz, and injects into bar, which it also drops.
        a: "baz = {\n\tcost = 2\n}\nINJECT:bar = {\n\tx = 1\n}\n",
        "in_game/common/building_types/m.txt": ("INJECT:foo = {\n\tx = 1\n}\nINJECT:baz = {\n\tx = 1\n}\n"
                                                "INJECT_OR_CREATE:foo = {\n\tx = 1\n}\n"),
    })
    with redirect_stdout(io.StringIO()) as buf:
        found = run_override_audit(mod, tr.repo, tr.hashes["1.0"], "1.0 Test", tr.hashes["1.1"], "1.1 Test",
                                   audit_args(), make_ctx(tr.repo, "1.1"))
    shadowed = sorted((f.name, f.location) for f in found if f.kind == "override_target_shadowed")
    assert shadowed == [("bar", f"{a}:4"), ("foo", "in_game/common/building_types/m.txt:1")]
    assert "Targets Only in a Vanilla File the Mod Replaces (2)" in buf.getvalue()
