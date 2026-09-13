"""Findings and the cross-audit triage. Runners return Finding items tagged
with a problem CLASS (report.KIND); render_triage groups them by class, states
each class's description and single remedy once, then lists the items, so the
summary grows one short line per real finding rather than one paragraph."""
import io
import re
from contextlib import redirect_stdout

from pdxaudit.report import (Finding, render_triage, finding_severity, SEV_INFO, KIND)
from pdxaudit.overrides import run_override_audit, run_deps_audit
from pdxaudit.gui import run_gui_audit
from pdxaudit.loc import run_loc_audit


def _run(fn, *a):
    """Call a runner, returning (findings, printed_detail)."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        findings = fn(*a)
    return findings, buf.getvalue()


# --- render_triage (pure) ---------------------------------------------------

def _k(name):
    return {"target": f"override:cat/{name}", "slot": "cost"}


def test_render_triage_states_each_class_fix_once():
    fs = [
        Finding("override_replace_frozen", "A", "m/a.txt:1", "", None, _k("A")),
        Finding("override_replace_frozen", "B", "m/b.txt:2", "", None, _k("B")),
        Finding("deps_ref_dropped", "x", "r.txt:3", "dropped in 1.1", None, {"target": "deps:x"}),
    ]
    out = render_triage(fs, "1.0.0", "1.1.0", ["overrides", "deps"])
    assert "3 findings need attention" in out
    # the class is described once with a single shared Fix line for both items
    assert "2 values your REPLACE keeps at vanilla's old value" in out
    assert out.count("Fix: take vanilla's new value") == 1
    assert re.search(r"- \[[0-9a-f]{8}\] `A` `m/a.txt:1`", out)
    assert re.search(r"- \[[0-9a-f]{8}\] `B` `m/b.txt:2`", out)
    # most urgent class first; closer points at the top tier present
    assert out.index("values your REPLACE") < out.index("references")
    assert "Open the stale items first" in out


def test_render_triage_lists_per_item_detail_for_flat_classes():
    out = render_triage(
        [Finding("deps_ref_dropped", "building_farm", "r.txt:2", "dropped in 1.1.0",
                 None, {"target": "deps:building_farm"})],
        "", "", ["deps"])
    assert re.search(r"- \[[0-9a-f]{8}\] `building_farm` `r.txt:2` \(dropped in 1.1.0\)", out)


def test_render_triage_clean_when_nothing_actionable():
    out = render_triage([Finding("gui_reconciled", "z", "f:3")],
                        "1.0.0", "1.1.0", ["gui"])
    assert "No action needed" in out
    assert "Fix:" not in out


def test_render_triage_summary_mode_points_away_from_detail():
    out = render_triage([Finding("override_replace_frozen", "x", "m/x.txt:1")],
                        "", "", ["overrides"], detail_shown=False)
    assert "without --summary" in out
    assert "follows below" not in out


def _line_finding(kind, old, new, mod, since="1.3.8", path=(), slot="requires", ops=("=", "=", "=")):
    key = {"target": "override:cat/blast_furnace", "path": list(path), "slot": slot,
           "op": "=", "ops": list(ops), "old": old, "new": new, "mod": mod}
    return Finding(kind, "blast_furnace", "m.txt:1", "detail", None, key, since)


def _value_lines(out):
    lines = out.splitlines()
    i = next(n for n, l in enumerate(lines) if "yours:" in l)
    return lines[i], lines[i + 1]


def test_triage_shows_values_on_labelled_aligned_lines():
    f = _line_finding("override_replace_frozen", "rgo_size_advance_absolutism",
                      "construction_speed_absolutism", "rgo_size_advance_absolutism")
    out = render_triage([f], "", "1.3.11 Pavia", ["overrides"], new_tag="1.3.11")
    yours, vanilla = _value_lines(out)
    assert yours.strip() == "yours:    rgo_size_advance_absolutism"
    assert vanilla.strip() == ("vanilla:  rgo_size_advance_absolutism  →  "
                               "construction_speed_absolutism  (1.3.8)")
    assert yours.index("rgo_size") == vanilla.index("rgo_size")      # value column aligned
    assert "(detail)" not in out


def test_triage_value_lines_per_class():
    cases = {
        ("override_replace_both_changed", "50", "200", "25"): ("**25**", "50  →  200  (1.3.8)"),
        ("override_replace_new_line", None, "5", None): ("(missing)", "added upkeep = 5  (1.3.8)"),
        ("override_replace_kept_removed", "1", None, "1"): ("upkeep = 1", "deleted  (1.3.8)"),
    }
    for (kind, old, new, mod), (want_yours, want_vanilla) in cases.items():
        f = _line_finding(kind, old, new, mod, slot="upkeep" if kind != "override_replace_both_changed" else "gold")
        out = render_triage([f], "", "1.3.11 Pavia", ["overrides"], new_tag="1.3.11")
        yours, vanilla = _value_lines(out)
        assert yours.strip() == f"yours:    {want_yours}", kind
        assert vanilla.strip() == f"vanilla:  {want_vanilla}", kind


def test_triage_value_lines_show_operators_when_they_differ():
    f = _line_finding("override_replace_frozen", "culture:x", "culture:x", "culture:x",
                      slot="culture", ops=("=", "?=", "="))
    out = render_triage([f], "", "1.3.11 Pavia", ["overrides"], new_tag="1.3.11")
    yours, vanilla = _value_lines(out)
    assert yours.strip() == "yours:    = culture:x"
    assert vanilla.strip() == "vanilla:  = culture:x  →  ?= culture:x  (1.3.8)"


def test_triage_prints_the_dismiss_command_with_a_real_id():
    from pdxaudit.ledger import finding_id, short_id
    f = _line_finding("override_replace_frozen", "a", "b", "a")
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


def test_no_kind_suggests_renames():
    for _sev, _audit, label, fix in KIND.values():
        assert "rename" not in f"{label} {fix}".lower() or "rename yours" in fix


# --- runners return findings (end to end) -----------------------------------

def test_gui_audit_does_not_flag_load_order(world):
    # zzz_mod.gui sorts after vanilla.gui, but a mod loads after vanilla and
    # overrides it regardless of filename, so this is NOT a problem. `bar` is
    # unchanged, so it yields no finding; `foo` drifted, so it is stale.
    findings, out = _run(run_gui_audit, world.mod, world.repo,
                         world.old, "1.0.0", world.new, "1.1.0", world.args)
    assert not any(f.kind == "gui_dead_shadow" for f in findings)
    assert "never apply" not in out and "load order" not in out.lower()
    assert any(f.kind == "gui_shadow_stale" and f.name == "foo" for f in findings)
    assert not any(f.name == "bar" for f in findings)   # unchanged shadow, no finding


def test_override_audit_compares_unchanged_script_value_quietly(world):
    # REPLACE:my_value targets a single-value script value vanilla did not
    # change: it is compared by value and produces no finding at all.
    findings, out = _run(run_override_audit, world.mod, world.repo,
                         world.old, "1.0.0", world.new, "1.1.0", world.args)
    assert not any(f.name == "my_value" for f in findings)
    assert "Not Found in Vanilla" not in out


def test_override_audit_stale_replace_lines_are_stale_classes(world):
    findings, _ = _run(run_override_audit, world.mod, world.repo,
                       world.old, "1.0.0", world.new, "1.1.0", world.args)
    kinds = {f.kind for f in findings if f.name == "some_building"}
    assert "override_replace_new_line" in kinds        # upkeep = 5
    assert "override_replace_kept_removed" in kinds    # legacy_mod = 1


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
