"""The command line end to end against the synthetic tracker: argument rules,
dismissals, the per-user report location, and that no run writes into the mod."""
import io
import re
import sys
from contextlib import redirect_stdout, redirect_stderr

import pytest

import pdxaudit.config as config
from pdxaudit.cli import main


@pytest.fixture
def cli(world, tmp_path, monkeypatch):
    data = tmp_path / "xdg"
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    monkeypatch.setenv("PDX_GAME_ROOT", str(tmp_path / "no-game"))
    monkeypatch.setattr(config, "_CACHE", {})

    def run(*argv, mod=None):
        monkeypatch.setattr(sys, "argv", [
            "pdx-audit", "--mod-root", str(mod or world.mod),
            "--vanilla-repo", world.repo, "--color", "never", *argv])
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with redirect_stdout(out), redirect_stderr(err):
            try:
                main()
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        return code, out.getvalue(), err.getvalue()

    run.data = data
    run.world = world
    return run


def _files(root):
    return sorted((p.relative_to(root).as_posix(), p.stat().st_size)
                  for p in root.rglob("*") if p.is_file())


def _id_for(out, name):
    for line in out.splitlines():
        m = re.search(r"\[([0-9a-f]{8})\]", line)
        if m and name in line:
            return m.group(1)
    raise AssertionError(f"no id printed for {name}:\n{out}")


def test_force_requires_remove_orphaned_records(cli):
    code, _out, err = cli("--force")
    assert code == 2 and "--force" in err


def test_reason_requires_dismiss(cli):
    code, _out, err = cli("--reason", "x")
    assert code == 2 and "--reason" in err


def test_dismiss_hides_then_undismiss_restores(cli):
    code, out, _ = cli("--overrides")
    assert code == 0
    fid = _id_for(out, "some_building")

    code, out, _ = cli("--overrides", "--dismiss", fid, "--reason", "kept on purpose")
    assert code == 0 and "Dismissed" in out and fid in out

    code, out, _ = cli("--overrides")
    assert f"[{fid}]" not in out
    assert "1 dismissed" in out

    code, out, _ = cli("--show-dismissed")
    assert fid in out and "kept on purpose" in out

    code, out, _ = cli("--undismiss", fid)
    assert code == 0 and "Restored" in out

    code, out, _ = cli("--overrides")
    assert f"[{fid}]" in out


def test_dismiss_unknown_id_fails(cli):
    code, _out, err = cli("--overrides", "--dismiss", "00000000")
    assert code == 1 and "no current finding" in err


def test_results_file_is_written_where_asked(cli):
    path = cli.data.parent / "results.json"
    code, _out, _ = cli("--overrides", "--results-file", str(path))
    assert code == 0 and path.is_file()


def test_runs_never_write_into_the_mod(cli):
    before = _files(cli.world.mod)
    cli()
    cli("--overrides", "--results-file", str(cli.data.parent / "results.json"))
    code, out, _ = cli("--overrides")
    fid = re.search(r"\[([0-9a-f]{8})\]", out).group(1)
    cli("--overrides", "--dismiss", fid)
    assert _files(cli.world.mod) == before


def test_backwards_window_is_an_error(cli):
    code, _out, err = cli("--old", "1.1.0", "--new", "1.0.0")
    assert code == 2 and "older" in err


def test_equal_window_warns(cli):
    code, _out, err = cli("--overrides", "--old", "1.1.0", "--new", "1.1.0")
    assert code == 0 and "same version" in err


def test_block_matching_nothing_exits_2(cli):
    code, _out, err = cli("--block", "no_such_block_xyz")
    assert code == 2 and "no_such_block_xyz" in err


def test_category_only_with_overrides_or_dupes(cli):
    code, _out, err = cli("--category", "building_types")
    assert code == 2 and "--category" in err
    code, _out, _err = cli("--overrides", "--category", "building_types")
    assert code == 0


def test_block_skips_the_dependency_audit(cli):
    code, out, _ = cli("--block", "some_building")
    assert code == 0
    assert "Ran override, GUI, localization, duplicate." in out


def test_duplicates_cannot_be_dismissed(cli):
    code, out, _ = cli("--dupes")
    assert "dup_thing" in out
    line = next(l for l in out.splitlines() if "`dup_thing`" in l)
    assert not re.search(r"\[[0-9a-f]{8}\]", line)        # no id offered
    code, out, err = cli("--dupes", "--dismiss", _dupe_id(cli))
    assert code == 1 and "cannot be dismissed" in err


def _dupe_id(cli):
    import io as _io
    from contextlib import redirect_stdout as _rs
    from conftest import make_ctx, audit_args
    from pdxaudit.dupes import run_dupes_audit
    from pdxaudit import ledger
    world = cli.world
    with _rs(_io.StringIO()):
        findings = run_dupes_audit(world.mod, world.repo, world.new, "1.1.0 Test",
                                   audit_args(), make_ctx(world.repo, "1.1.0"))
    f = next(f for f in findings if f.name == "dup_thing")
    return ledger.short_id(ledger.finding_id(f))


def _record(cli):
    import json
    p = cli.data / "pdx-audit" / "testmod" / "record.json"
    return json.loads(p.read_text()) if p.exists() else None


def test_default_run_records_open_findings_and_filtered_runs_leave_them(cli):
    cli()
    rec = _record(cli)
    assert rec and rec["open"]
    before = dict(rec["open"])
    cli("--block", "some_building")
    cli("--overrides", "--old", "1.0.0", "--new", "1.1.0")
    assert _record(cli)["open"] == before


def test_repeated_default_runs_report_the_same_findings(cli):
    _code, first, _ = cli()
    _code, second, _ = cli()
    ids = lambda out: sorted(re.findall(r"\[([0-9a-f]{8})\]", out))
    assert ids(first) and ids(first) == ids(second)


def test_dismissed_finding_is_not_open(cli):
    code, out, _ = cli("--overrides")
    fid = _id_for(out, "some_building")
    cli("--overrides", "--dismiss", fid)
    cli()
    rec = _record(cli)
    assert any(k.startswith(fid) for k in rec["dismissed"])
    assert not any(k.startswith(fid) for k in rec["open"])


def test_orphan_note_and_forced_removal_through_cli(cli, monkeypatch):
    import subprocess
    mod = cli.world.mod
    env_git = ["-c", "user.name=t", "-c", "user.email=t@e"]
    subprocess.run(["git", "-C", str(mod), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(mod), *env_git, "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(mod), *env_git, "commit", "-q", "-m", "m"], check=True)
    commits = cli.data / "pdx-audit" / "testmod" / "commits"
    commits.mkdir(parents=True)
    orphan = commits / ("f" * 40 + ".json")
    orphan.write_text("{}")
    code, _out, err = cli("--show-dismissed")
    assert "Note:" in err and "ffffffff" in err and "--remove-orphaned-records" in err
    code, out, _ = cli("--remove-orphaned-records", "--force")
    assert code == 0 and not orphan.exists() and "Removed 1" in out


def test_show_dismissed_when_empty(cli):
    code, out, _ = cli("--show-dismissed")
    assert code == 0 and "No dismissed findings" in out
