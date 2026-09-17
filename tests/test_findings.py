"""Findings and the cross-audit triage. Runners return Finding items tagged
with a problem CLASS (report.KIND); render_triage groups them by class, states
each class's description and single remedy once, then lists the items, so the
summary grows one short line per real finding rather than one paragraph."""
import io
import re
from contextlib import redirect_stdout

from pdxaudit.report import Finding, count_label, render_triage, KIND, SEV_REVIEW, SEV_STALE
from pdxaudit.overrides import run_override_audit, run_deps_audit
from pdxaudit.gui import run_gui_audit
from pdxaudit.loc import run_loc_audit


def _run(fn, *a):
    """Call a runner, returning (findings, printed_detail)."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        findings = fn(*a)
    return findings, buf.getvalue()


# --- the class registry -----------------------------------------------------

def test_change_classes_follow_priority():
    for audit in ("gui", "override"):
        for change in ("vanilla_changed", "vanilla_added", "vanilla_removed"):
            assert KIND[f"{audit}_{change}_high"][0] == SEV_STALE
            assert KIND[f"{audit}_{change}_mid"][0] == SEV_REVIEW
        assert KIND[f"{audit}_both_changed_mid"][0] == SEV_REVIEW
        assert f"{audit}_both_changed_high" not in KIND
        # A statement the copy deleted is its own edit whatever vanilla did to it after,
        # so nothing produces this kind any more; it stays for records written before.
        assert KIND[f"{audit}_removed_changed_mid"][0] == SEV_REVIEW
    assert KIND["override_inject_overlap"][0] == SEV_STALE


# --- render_triage (pure) ---------------------------------------------------

def _change(name, kind="override_vanilla_changed_mid", loc="m/a.txt:1", yours="cost = 1",
            vanilla="cost = 2", since="1.3.8", path=()):
    key = {"target": f"override:cat/{name}", "path": list(path), "yours": yours, "vanilla": vanilla}
    return Finding(kind, name, loc, " > ".join(path), None, key, since)


def test_render_triage_states_each_class_fix_once():
    fs = [
        _change("A", loc="m/a.txt:1"),
        _change("B", loc="m/b.txt:2"),
        Finding("deps_ref_dropped", "x", "r.txt:3", "dropped in 1.1", None, {"target": "deps:x"}),
    ]
    out = render_triage(fs, "1.0.0", "1.1.0", ["overrides", "deps"])
    assert "3 findings need attention" in out
    assert "2 statements your REPLACE keeps at an old vanilla value" in out
    assert out.count("Fix: take vanilla's new value") == 1
    assert re.search(r"- \[[0-9a-f]{8}\] `A` `m/a.txt:1`", out)
    assert re.search(r"- \[[0-9a-f]{8}\] `B` `m/b.txt:2`", out)
    assert "Open the review items first" in out


def test_a_class_of_one_is_named_in_the_singular():
    out = render_triage([_change("A")], "", "", ["overrides"])
    assert "1 statement your REPLACE keeps at an old vanilla value" in out
    assert count_label(1, KIND["dupes_on_action_syntax"][2]) == "on_action block that sets `effect` or `trigger` twice"
    assert count_label(1, KIND["dupes_gui_definition"][2]).startswith("GUI template or type defined")
    assert count_label(2, KIND["dupes_gui_definition"][2]) == KIND["dupes_gui_definition"][2]


def test_a_file_line_names_places_in_its_own_file_by_line():
    f = Finding("dupes_loc_key", "k", "loc/a.yml:3", "loc/a.yml:3; loc/b.yml:8", None,
                {"target": "dupes:loc/english/k"})
    out = render_triage([f], "", "", ["dupes"])
    assert "- `loc/a.yml`: `k` (line 3; loc/b.yml:8)" in out


def test_the_heading_names_both_windows_when_copies_reach_further_back():
    out = render_triage([_change("A")], "1.1.0 Test", "1.2.0 Test", ["overrides", "gui"], history_old="1.0.0 Test")
    assert out.startswith("# Audit summary: 1.2.0 Test\n")
    assert "every snapshot from 1.0.0 Test" in out and "INJECT targets from 1.1.0 Test" in out
    out = render_triage([_change("A")], "1.1.0 Test", "1.2.0 Test", ["gui"], history_old="1.0.0 Test")
    assert out.startswith("# Audit summary: 1.0.0 Test → 1.2.0 Test")
    out = render_triage([_change("A")], "1.0.0 Test", "1.2.0 Test", ["gui"], history_old="1.0.0 Test")
    assert out.startswith("# Audit summary: 1.0.0 Test → 1.2.0 Test")


def test_a_loc_change_across_a_wide_window_is_dated_by_its_version(world, tmp_path):
    from conftest import VANILLA_NEW, VANILLA_OLD, audit_args, build_tracker, make_ctx
    t = build_tracker(tmp_path / "three", [("1.0.0", VANILLA_OLD), ("1.0.5", VANILLA_NEW), ("1.1.0", VANILLA_NEW)])
    findings, _ = _run(run_loc_audit, world.mod, t.repo, t.hashes["1.0.0"], "1.0.0 Test", t.hashes["1.1.0"],
                       "1.1.0 Test", audit_args(full=True), make_ctx(t.repo, "1.1.0", fixed=True))
    assert [f.since for f in findings if f.kind == "loc_changed"] == ["1.0.5"]


def test_an_inject_overlap_is_dated_by_the_version_that_changed_its_key(world, tmp_path):
    from conftest import VANILLA_OLD, audit_args, build_tracker, make_ctx, _write_tree
    block = "in_game/common/building_types/b.txt"
    other_change = dict(VANILLA_OLD, **{block: "some_building = {\n\tcost = 100\n\tlegacy_mod = 1\n\tupkeep = 1\n}\n"})
    key_change = dict(VANILLA_OLD, **{block: "some_building = {\n\tcost = 200\n\tlegacy_mod = 1\n\tupkeep = 1\n}\n"})
    t = build_tracker(tmp_path / "three", [("1.0.0", VANILLA_OLD), ("1.0.5", other_change), ("1.1.0", key_change)])
    mod = tmp_path / "injmod"
    _write_tree(mod, {".metadata/metadata.json": '{"id":"inj"}',
                      "in_game/common/building_types/m.txt": "INJECT:some_building = {\n\tcost = 150\n}\n"})
    findings, _ = _run(run_override_audit, mod, t.repo, t.hashes["1.0.0"], "1.0.0 Test", t.hashes["1.1.0"],
                       "1.1.0 Test", audit_args(full=True), make_ctx(t.repo, "1.1.0", fixed=True))
    assert [f.since for f in findings if f.kind == "override_inject_overlap"] == ["1.1.0"]


def test_render_triage_lists_per_item_detail_for_flat_classes():
    out = render_triage(
        [Finding("deps_ref_dropped", "building_farm", "r.txt:2", "dropped in 1.1.0",
                 None, {"target": "deps:building_farm"})],
        "", "", ["deps"])
    assert re.search(r"- \[[0-9a-f]{8}\] `building_farm` `r.txt:2` \(dropped in 1.1.0\)", out)


def test_render_triage_clean_when_nothing_actionable():
    out = render_triage([Finding("override_inject_context", "z", "f:3")],
                        "1.0.0", "1.1.0", ["overrides"])
    assert "No action needed" in out
    assert "Fix:" not in out


def test_render_triage_summary_mode_points_away_from_detail():
    out = render_triage([_change("x")], "", "", ["overrides"], detail_shown=False)
    assert "without --summary" in out
    assert "follows below" not in out


def _value_lines(out):
    lines = out.splitlines()
    i = next(n for n, l in enumerate(lines) if "yours:" in l)
    return lines[i], lines[i + 1]


def test_triage_shows_values_on_labelled_aligned_lines():
    f = _change("blast_furnace", "override_vanilla_changed_high", yours="requires = a",
                vanilla="requires = b", path=("unlock",))
    out = render_triage([f], "", "1.3.11 Pavia", ["overrides"], new_tag="1.3.11")
    assert "`blast_furnace` `m/a.txt:1` unlock" in out
    yours, vanilla = _value_lines(out)
    assert yours.strip() == "yours:    requires = a"
    assert vanilla.strip() == "vanilla:  requires = a  →  requires = b  (1.3.8)"
    assert yours.index("requires") == vanilla.index("requires")      # value column aligned


def test_triage_value_lines_per_change():
    cases = {
        ("override_both_changed_mid", "gold = 25", "gold = 200"): ("**gold = 25**", "changed to gold = 200  (1.3.8)"),
        ("override_both_changed_mid", "gold = 25", None): ("**gold = 25**", "deleted  (1.3.8)"),
        ("override_vanilla_added_mid", None, "upkeep = 5"): ("(missing)", "added upkeep = 5  (1.3.8)"),
        ("override_vanilla_removed_mid", "upkeep = 1", None): ("upkeep = 1", "deleted  (1.3.8)"),
        ("override_removed_changed_mid", None, "upkeep = 2"): ("(removed)", "changed to upkeep = 2  (1.3.8)"),
    }
    for (kind, yours, vanilla), (want_yours, want_vanilla) in cases.items():
        out = render_triage([_change("b", kind, yours=yours, vanilla=vanilla)], "", "1.3.11 Pavia",
                            ["overrides"], new_tag="1.3.11")
        got_yours, got_vanilla = _value_lines(out)
        assert got_yours.strip() == f"yours:    {want_yours}", kind
        assert got_vanilla.strip() == f"vanilla:  {want_vanilla}", kind


def test_a_conflict_shows_vanillas_text_before_its_change():
    cases = {
        ("override_both_changed_mid", "gold = 25", "gold = 200"): "gold = 100  →  gold = 200  (1.3.8)",
        ("override_both_changed_mid", "gold = 25", None): "deleted gold = 100  (1.3.8)",
        ("override_removed_changed_mid", None, "gold = 200"): "gold = 100  →  gold = 200  (1.3.8)",
    }
    for (kind, yours, vanilla), want in cases.items():
        f = _change("b", kind, yours=yours, vanilla=vanilla)
        f = f._replace(key=dict(f.key, was="gold = 100"))
        out = render_triage([f], "", "1.3.11 Pavia", ["overrides"], new_tag="1.3.11")
        assert _value_lines(out)[1].strip() == f"vanilla:  {want}", kind


def test_triage_prints_the_dismiss_command_with_a_real_id():
    from pdxaudit.ledger import finding_id, short_id
    f = _change("a")
    out = render_triage([f], "", "1.3.11 Pavia", ["overrides"], new_tag="1.3.11")
    sid = short_id(finding_id(f))
    assert f'pdx-audit --dismiss {sid} --reason "why"' in out
    assert "--undismiss" in out
    assert "until vanilla's value or yours changes" in out


def test_triage_offers_no_dismiss_command_for_duplicates():
    dup = Finding("dupes_multiple_sources", "toll_castle", "a.txt:3", "", None,
                  {"target": "dupes:common/building_types/toll_castle"})
    out = render_triage([dup], "", "1.3.11 Pavia", ["dupes"], new_tag="1.3.11")
    assert "--dismiss" not in out
    assert "cannot be dismissed" in out


def test_triage_lists_file_classes_one_line_per_mod_file():
    loc = "main_menu/localization/english"
    fs = [Finding("dupes_loc_key", key, f"{loc}/{file}:{line}", "", None, {"target": f"dupes:loc/english/{key}"})
          for key, file, line in (("A", "a_l_english.yml", 2), ("B", "a_l_english.yml", 9),
                                  ("C", "b_l_english.yml", 4))]
    out = render_triage(fs, "", "1.3.11 Pavia", ["dupes"], new_tag="1.3.11")
    assert out.count("Fix: keep one copy and delete the others.") == 1
    assert f"  - `{loc}/a_l_english.yml`: `A`, `B`" in out
    assert f"  - `{loc}/b_l_english.yml`: `C`" in out
    assert "--dismiss" not in out


def test_classes_after_a_per_file_class_are_all_listed():
    loc = "main_menu/localization/english"
    fs = [Finding("dupes_loc_key", "A", f"{loc}/a_l_english.yml:2", "", None, {"target": "dupes:loc/english/A"}),
          Finding("dupes_loc_key", "B", f"{loc}/b_l_english.yml:2", "", None, {"target": "dupes:loc/english/B"}),
          Finding("dupes_on_action_key", "on_x", "in_game/common/on_action/a.txt:2", "", None,
                  {"target": "dupes:common/on_action/on_x/effect"}),
          Finding("dupes_loc_key_same", "C", f"{loc}/c_l_english.yml:2", "", None, {"target": "dupes:loc/english/C"})]
    out = render_triage(fs, "", "1.1 Test", ["dupes"], new_tag="1.1")
    for name in ("A", "B", "on_x", "C"):
        assert f"`{name}`" in out, name


def test_findings_against_an_adopted_source_are_worded_as_its_own():
    theirs = _change("w", "gui_vanilla_changed_mid", since="1.1", yours="size = 1", vanilla="size = 2")
    theirs = theirs._replace(key=dict(theirs.key, target="gui:in_game/template/w", base="up"))
    mine = _change("x", "gui_vanilla_changed_mid", since="1.3.11")
    out = render_triage([theirs, mine], "", "1.3.11 Test", ["gui", "adopted"], new_tag="1.3.11", adopted={"up"})
    assert "1 statement your copy keeps at an old up value" in out
    assert "1 statement your GUI copy keeps at an old vanilla value" in out
    assert "upstream: size = 1  →  size = 2  (1.1)" in out
    assert "Still open from earlier patches" not in out


def test_points_placed_on_the_newest_patch_count_as_this_patch():
    out = render_triage([_change("a", since="found 2.1"), _change("b", since="1.3.10")], "", "", ["overrides"],
                        new_tag="found 2.1", patch_tags={"1.3.11", "found 2.1"})
    assert "## This patch (1.3.11)" in out
    assert out.index("`a`") < out.index("Still open from earlier patches") < out.index("`b`")


def test_no_kind_suggests_renames():
    for _sev, _audit, label, fix in KIND.values():
        assert "rename" not in f"{label} {fix}".lower() or "rename yours" in fix


# --- runners return findings (end to end) -----------------------------------

def test_gui_audit_does_not_flag_load_order(world):
    # zzz_mod.gui sorts after vanilla.gui, but a mod loads after vanilla and
    # overrides it regardless of filename, so this is NOT a problem. `bar` is
    # unchanged, so it yields no finding; `foo` lacks vanilla's change.
    findings, out = _run(run_gui_audit, world.mod, world.repo,
                         world.old, "1.0.0", world.new, "1.1.0", world.args)
    assert not any(f.kind == "gui_dead_shadow" for f in findings)
    assert "never apply" not in out and "load order" not in out.lower()
    assert any(f.kind == "gui_vanilla_changed_mid" and f.name == "foo" for f in findings)
    assert not any(f.name == "bar" for f in findings)


def test_override_audit_compares_unchanged_script_value_quietly(world):
    # REPLACE:my_value targets a single-value script value vanilla did not
    # change: it is compared by value and produces no finding at all.
    findings, out = _run(run_override_audit, world.mod, world.repo,
                         world.old, "1.0.0", world.new, "1.1.0", world.args)
    assert not any(f.name == "my_value" for f in findings)
    assert "Not Found in Vanilla" not in out


def test_override_audit_reports_the_replaces_missing_changes(world):
    findings, _ = _run(run_override_audit, world.mod, world.repo,
                       world.old, "1.0.0", world.new, "1.1.0", world.args)
    kinds = {f.kind for f in findings if f.name == "some_building"}
    assert kinds == {"override_vanilla_added_mid", "override_vanilla_removed_mid"}


def test_loc_audit_returns_changed_class(world):
    findings, _ = _run(run_loc_audit, world.mod, world.repo,
                       world.old, "1.0.0", world.new, "1.1.0", world.args)
    assert any(f.kind == "loc_changed" and f.name == "KEY_A" for f in findings)


def test_deps_audit_returns_dropped_classes(world):
    findings, _ = _run(run_deps_audit, world.mod, world.repo,
                       world.old, "1.0.0", world.new, "1.1.0")
    kinds = {f.kind for f in findings}
    assert "deps_key_dropped" in kinds       # legacy_mod, a key the mod writes
    assert "deps_ref_dropped" in kinds       # building_farm, a referenced name


def test_every_finding_has_a_fingerprint_target(world):
    for fn, extra in ((run_override_audit, (world.args,)), (run_gui_audit, (world.args,)),
                      (run_loc_audit, (world.args,)), (run_deps_audit, ())):
        findings, _ = _run(fn, world.mod, world.repo, world.old, "1.0.0",
                           world.new, "1.1.0", *extra)
        for f in findings:
            assert f.key and f.key.get("target"), f
