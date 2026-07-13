"""End-to-end tests against a synthetic tracker (see the `world` fixture):
the git layer plus each audit, driven the way the CLI drives them."""
import io
from contextlib import redirect_stdout

from pdxaudit.tracker import get_commits, resolve_ref
from pdxaudit.overrides import run_override_audit, run_deps_audit
from pdxaudit.gui import run_gui_audit
from pdxaudit.loc import run_loc_audit


def _out(fn, *a):
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn(*a)
    return buf.getvalue()


def test_tracker_commits_newest_first(world):
    commits = get_commits(world.repo)          # short hashes, newest first
    assert len(commits) == 2
    assert world.new.startswith(commits[0][0])
    assert world.old.startswith(commits[1][0])
    assert resolve_ref(world.repo, "1.1.0", commits, "new") == "1.1.0 Test"


def test_override_audit_flags_stale_replace(world):
    out = _out(run_override_audit, world.mod, world.repo,
               world.old, "1.0.0", world.new, "1.1.0", world.args)
    # vanilla added `upkeep = 5`; the mod's REPLACE lacks it -> stale
    assert "some_building" in out
    assert "1 REPLACE blocks stale" in out
    assert "upkeep = 5" in out          # names the missing line


def test_deps_audit_flags_dropped_key(world):
    out = _out(run_deps_audit, world.mod, world.repo,
               world.old, "1.0.0", world.new, "1.1.0")
    # vanilla dropped `legacy_mod`, which the mod still writes as a key
    assert "legacy_mod" in out
    assert "**1** keys the mod writes that vanilla dropped" in out


def test_deps_audit_flags_dropped_reference(world):
    out = _out(run_deps_audit, world.mod, world.repo,
               world.old, "1.0.0", world.new, "1.1.0")
    # vanilla renamed building_farm -> building_granary; the mod references the
    # old name on the right-hand side of `has_building = building_farm`
    assert "**1** names the mod references that vanilla dropped" in out
    assert "building_farm" in out
    assert "building_granary" in out          # offered as a rename candidate


def test_gui_audit_flags_stale_shadow(world):
    out = _out(run_gui_audit, world.mod, world.repo,
               world.old, "1.0.0", world.new, "1.1.0", world.args)
    # vanilla changed template `foo`; the mod's shadow copy is behind
    assert "foo" in out
    assert "1 shadowed definitions drifted" in out


def test_loc_audit_flags_changed_string(world):
    out = _out(run_loc_audit, world.mod, world.repo,
               world.old, "1.0.0", world.new, "1.1.0", world.args)
    # vanilla reworded KEY_A; the mod overrides it
    assert "KEY_A" in out
    assert "old text" in out and "new text" in out
    assert "1 changed strings" in out


def test_clean_when_mod_matches_new_vanilla(world):
    # point old and new at the same commit: nothing changed underneath -> no findings
    out = _out(run_override_audit, world.mod, world.repo,
               world.new, "1.1.0", world.new, "1.1.0", world.args)
    assert "unique overrides scanned" in out
    assert "1 REPLACE blocks stale" not in out
