"""End-to-end tests against a synthetic tracker (see the `world` fixture):
the git layer plus each audit, driven the way the CLI drives them."""
import io
import types
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


def test_a_snapshot_with_no_tag_resolves_by_the_name_it_is_shown_by(world):
    # The first snapshot of a hand-built tracker can carry no tag; the app's version
    # list and --list-commits still show it by the first word of its message, and
    # --old/--new have to accept what they show.
    untagged = "Pre-patch snapshot — txt/yml/gui only"
    commits = [("aaaaaaa", "1.1.0 Test"), ("bbbbbbb", untagged)]
    assert resolve_ref(world.repo, "Pre-patch", commits, "old") == untagged


def test_a_ref_that_matches_nothing_still_suggests_the_near_ones(world, capsys):
    import pytest
    commits = [("aaaaaaa", "1.1.0 Test"), ("bbbbbbb", "1.2.0 Test")]
    with pytest.raises(SystemExit):
        resolve_ref(world.repo, "1.1.5", commits, "old")
    assert "did you mean: 1.1.0" in capsys.readouterr().err


def test_override_audit_flags_stale_replace(world):
    out = _out(run_override_audit, world.mod, world.repo,
               world.old, "1.0.0", world.new, "1.1.0", world.args)
    # vanilla added `upkeep = 5` and dropped `legacy_mod`; the mod's REPLACE
    # lacks the first and still carries the second
    assert "some_building" in out
    assert "2 REPLACE changes to take or check" in out
    assert "upkeep = 5" in out and "legacy_mod = 1" in out


def test_deps_audit_flags_dropped_key(world):
    out = _out(run_deps_audit, world.mod, world.repo,
               world.old, "1.0.0", world.new, "1.1.0")
    # vanilla dropped `legacy_mod`, which the mod still writes as a key
    assert "legacy_mod" in out
    assert "**1** keys the mod writes that vanilla no longer uses" in out


def test_deps_audit_flags_dropped_reference(world):
    out = _out(run_deps_audit, world.mod, world.repo,
               world.old, "1.0.0", world.new, "1.1.0")
    # vanilla replaced building_farm with building_granary; the mod references
    # the old name on the right-hand side of `has_building = building_farm`
    assert "**1** names the mod references that vanilla no longer uses" in out
    assert "building_farm" in out
    assert "building_granary" not in out          # no rename suggestions


def test_gui_audit_flags_stale_shadow(world):
    out = _out(run_gui_audit, world.mod, world.repo,
               world.old, "1.0.0", world.new, "1.1.0", world.args)
    # vanilla changed template `foo`; the mod's shadow copy lacks the change
    assert "foo" in out
    assert "1 changes in 1 shadowed definitions" in out


def test_loc_audit_flags_changed_string(world):
    out = _out(run_loc_audit, world.mod, world.repo,
               world.old, "1.0.0", world.new, "1.1.0", world.args)
    # vanilla reworded KEY_A; the mod overrides it
    assert "KEY_A" in out
    assert "old text" in out and "new text" in out
    assert "1 changed strings" in out


def test_clean_when_mod_matches_new_vanilla(world):
    # a window of the new version alone has no history to attribute changes with
    args = types.SimpleNamespace(**dict(vars(world.args), full=False, old="1.1.0"))
    out = _out(run_override_audit, world.mod, world.repo,
               world.new, "1.1.0", world.new, "1.1.0", args)
    assert "unique overrides scanned" in out
    assert "Action needed" not in out
