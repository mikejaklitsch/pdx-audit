"""Findings and the cross-audit triage. Runners return Finding items tagged
with a problem CLASS (report.KIND); render_triage groups them by class, states
each class's description and single remedy once, then lists the items, so the
summary grows one short line per real finding rather than one paragraph."""
import io
from contextlib import redirect_stdout

from pdxaudit.report import (Finding, render_triage, finding_severity, SEV_INFO)
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

def test_render_triage_states_each_class_fix_once():
    fs = [
        Finding("override_replace_stale", "A", "m/a.txt:1"),
        Finding("override_replace_stale", "B", "m/b.txt:2"),
        Finding("deps_ref_dropped", "x", "r.txt:3", "maybe y?"),
    ]
    out = render_triage(fs, "1.0.0", "1.1.0", ["overrides", "deps"])
    assert "3 findings need attention" in out
    # the class is described once with a single shared Fix line for both items
    assert "2 REPLACE blocks that no longer carry" in out
    assert out.count("Fix: merge vanilla's change") == 1
    assert "- `A` `m/a.txt:1`" in out and "- `B` `m/b.txt:2`" in out
    # most urgent class first; closer points at the top tier present
    assert out.index("REPLACE blocks") < out.index("references")
    assert "Open the stale items first" in out


def test_render_triage_lists_per_item_detail_for_flat_classes():
    out = render_triage(
        [Finding("deps_ref_dropped", "building_farm", "r.txt:2", "maybe building_granary?")],
        "", "", ["deps"])
    assert "- `building_farm` `r.txt:2` (maybe building_granary?)" in out


def test_render_triage_clean_when_nothing_actionable():
    out = render_triage([Finding("gui_reconciled", "z", "f:3")],
                        "1.0.0", "1.1.0", ["gui"])
    assert "No action needed" in out
    assert "Fix:" not in out


def test_render_triage_summary_mode_points_away_from_detail():
    out = render_triage([Finding("override_replace_stale", "x", "m/x.txt:1")],
                        "", "", ["overrides"], detail_shown=False)
    assert "without --summary" in out
    assert "follows below" not in out


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


def test_override_audit_buckets_script_value_as_info(world):
    # REPLACE:my_value targets a script value, not a top-level block: it belongs
    # in the informational "defined but not a block" class, not a scary miss.
    findings, out = _run(run_override_audit, world.mod, world.repo,
                         world.old, "1.0.0", world.new, "1.1.0", world.args)
    hit = next(f for f in findings if f.name == "my_value")
    assert hit.kind == "override_nonblock"
    assert finding_severity(hit) == SEV_INFO
    assert "Not as a Top-Level Block" in out
    assert "Not Found in Vanilla" not in out     # not the genuinely-absent bucket


def test_override_audit_stale_replace_is_a_stale_class(world):
    findings, _ = _run(run_override_audit, world.mod, world.repo,
                       world.old, "1.0.0", world.new, "1.1.0", world.args)
    assert any(f.kind == "override_replace_stale" and f.name == "some_building"
               for f in findings)


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
