"""The command line end to end against the synthetic tracker: argument rules,
dismissals, the per-user report location, and that no run writes into the mod."""
import io
import json
import re
import sys
import types
from contextlib import redirect_stdout, redirect_stderr

import pytest

import pdxaudit.config as config
from conftest import _write_tree, build_tracker
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


def test_a_run_of_one_audit_keeps_the_other_audits_open_findings(cli):
    cli()
    before = _record(cli)["open"]
    assert {e["finding"].split("_")[0] for e in before.values()} > {"gui"}
    cli("--gui")
    assert _record(cli)["open"] == before


def test_a_dismissal_the_last_run_no_longer_found_is_marked(cli):
    code, out, _ = cli("--overrides")
    fid = _id_for(out, "some_building")
    cli("--overrides", "--dismiss", fid)
    (cli.world.mod / "in_game/common/building_types/m.txt").write_text(
        "REPLACE:some_building = {\n\tcost = 100\n\tupkeep = 5\n}\n", encoding="utf-8")
    cli("--overrides")
    code, out, _ = cli("--show-dismissed")
    assert fid in out and "no longer found" in out


def test_new_alone_compares_from_the_version_before_it(cli, tmp_path):
    from conftest import VANILLA_NEW, VANILLA_OLD, build_tracker
    t = build_tracker(tmp_path / "three", [("1.0.0", VANILLA_OLD), ("1.0.5", VANILLA_OLD), ("1.1.0", VANILLA_NEW)])
    code, _out, err = cli("--deps", "--new", "1.0.5", "--vanilla-repo", t.repo)
    assert code == 0 and "must be older" not in err and "same version" not in err


@pytest.mark.parametrize("argv, text", [
    (("--full", "--old", "1.0.0"), "--full and --old"),
    (("--diff", "--summary"), "--diff and --summary"),
    (("--deps", "--block", "some_building"), "dependency audit"),
])
def test_options_that_cannot_work_together_are_refused(cli, argv, text):
    code, _out, err = cli(*argv)
    assert code == 2 and text in err


def test_a_mod_root_that_is_not_a_mod_is_refused(cli, tmp_path):
    code, out, err = cli("--summary", mod=tmp_path / "nowhere")
    assert code == 1 and "not found" in err and not out
    (tmp_path / "plain").mkdir()
    code, out, err = cli("--summary", mod=tmp_path / "plain")
    assert code == 1 and ".metadata" in err and not out


def test_a_vanilla_repo_setting_that_points_nowhere_is_an_error(world, tmp_path, monkeypatch):
    from pdxaudit.tracker import find_vanilla_repo
    monkeypatch.setenv("PDX_VANILLA_REPO", str(tmp_path / "missing"))
    with pytest.raises(SystemExit) as e:
        find_vanilla_repo(world.mod)
    assert e.value.code == 1


def test_repeated_default_runs_report_the_same_findings(cli):
    _code, first, _ = cli()
    _code, second, _ = cli()
    ids = lambda out: sorted(re.findall(r"\[([0-9a-f]{8})\]", out))
    assert ids(first) and ids(first) == ids(second)


def test_a_replace_of_an_injected_block_never_widens_the_inject_check_on_a_later_run(cli, tmp_path):
    b = "in_game/common/building_types/b.txt"
    tr = build_tracker(tmp_path / "t", [("1.0", {b: "thing = {\n\tcost = 1\n\tupkeep = 1\n}\n"}),
                                        ("1.1", {b: "thing = {\n\tcost = 2\n\tupkeep = 2\n}\n"}),
                                        ("1.2", {b: "thing = {\n\tcost = 2\n\tupkeep = 2\n\tx = 1\n}\n"})])
    mod = tmp_path / "injmod"
    _write_tree(mod, {".metadata/metadata.json": '{"name": "Inj", "id": "injmod"}',
                      "in_game/common/building_types/r.txt": "REPLACE:thing = {\n\tcost = 1\n\tupkeep = 1\n}\n",
                      "in_game/common/building_types/i.txt": "INJECT:thing = {\n\tupkeep = 9\n}\n"})
    runs = [cli("--vanilla-repo", tr.repo, "--overrides", mod=mod)[1] for _ in range(3)]
    ids = lambda out: sorted(re.findall(r"\[([0-9a-f]{8})\]", out))
    assert ids(runs[0]) and ids(runs[0]) == ids(runs[1]) == ids(runs[2])
    assert "INJECT targets where vanilla also changed" not in runs[1]


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


# --- the config commands ----------------------------------------------------------


@pytest.fixture
def cfg_cli(cli, tmp_path, monkeypatch):
    """`cli` with the config file under tmp_path, so --set writes nothing real."""
    monkeypatch.delenv("PDX_GAME_ROOT", raising=False)
    data = tmp_path / "data.json"
    monkeypatch.setattr(config, "writable_path", lambda: data)
    monkeypatch.setattr(config, "_former_paths", lambda: [])
    monkeypatch.setattr(config, "_CACHE", None)
    cli.config = types.SimpleNamespace(data=data)
    return cli


def test_config_lists_every_setting_and_the_one_file(cfg_cli):
    code, out, _ = cfg_cli("--config")
    assert code == 0
    assert "`vanilla_repo`" in out and "`patch_name`" in out
    assert f"Config file: {cfg_cli.config.data}" in out and "(not present)" in out


def test_set_and_unset_a_tracker_under_any_name(cfg_cli, tmp_path):
    tracker = tmp_path / "eu5-history.git"
    (tracker / "objects").mkdir(parents=True)
    (tracker / "HEAD").write_text("ref: refs/heads/master\n")
    code, out, _ = cfg_cli("--set", "vanilla_repo", str(tracker))
    assert code == 0 and f"Set vanilla_repo to {tracker}" in out
    assert json.loads(cfg_cli.config.data.read_text())["vanilla_repo"] == str(tracker)
    code, out, _ = cfg_cli("--config")
    assert str(tracker) in out
    code, out, _ = cfg_cli("--unset", "vanilla_repo")
    assert code == 0 and "Removed vanilla_repo" in out
    assert "vanilla_repo" not in json.loads(cfg_cli.config.data.read_text())


def test_set_reports_a_bad_value_and_writes_nothing(cfg_cli, tmp_path):
    code, _out, err = cfg_cli("--set", "game_root", str(tmp_path / "missing"))
    assert code == 1 and "not a folder" in err
    assert not cfg_cli.config.data.exists()


def test_a_config_command_runs_on_its_own(cfg_cli):
    code, _out, err = cfg_cli("--config", "--display")
    assert code == 2 and "does its own thing and exits" in err
    code, _out, err = cfg_cli("--config", "--unset", "patch_name")
    assert code == 2 and "cannot be combined" in err


def test_patch_name_for_a_snapshot_comes_from_the_config(cfg_cli, tmp_path, monkeypatch):
    cfg_cli.config.data.write_text('{"patch_name": "Cortes"}')
    monkeypatch.setattr(config, "_CACHE", None)
    seen = {}
    monkeypatch.setattr("pdxaudit.cli.do_snapshot",
                        lambda repo, tag, patch, game=None: seen.update(patch=patch, tag=tag))
    assert cfg_cli("--snapshot", "1.3.12")[0] == 0
    assert seen == {"patch": "Cortes", "tag": "1.3.12"}
    monkeypatch.setenv("PDX_PATCH_NAME", "FromEnv")
    cfg_cli("--snapshot", "1.3.13")
    assert seen["patch"] == "FromEnv"
    cfg_cli("--snapshot", "1.3.14", "--patch-name", "FromFlag")
    assert seen["patch"] == "FromFlag"


def test_a_config_command_does_not_swallow_another_command(cfg_cli):
    for argv, named in ((("--config", "--list-commits"), "--list-commits"),
                        (("--config", "--overrides"), "--overrides"),
                        (("--set", "patch_name", "X", "--dismiss", "abc12345"), "--dismiss")):
        code, out, err = cfg_cli(*argv)
        assert code == 2 and named in err and "does its own thing and exits" in err
        assert "## Config" not in out


def test_set_refuses_an_empty_value_instead_of_storing_the_working_directory(cfg_cli):
    code, _out, err = cfg_cli("--set", "game_root", "")
    assert code == 1 and "needs a value" in err
    assert not cfg_cli.config.data.exists()


def test_a_snapshot_creates_the_tracker_in_a_folder_that_already_exists(cfg_cli, tmp_path):
    # A folder dialog can only return a folder that exists, so an empty one must work.
    game = tmp_path / "game" / "in_game" / "common" / "x"
    game.mkdir(parents=True)
    (game / "a.txt").write_text("a = {\n\tb = 1\n}\n")
    empty = tmp_path / "picked-in-a-dialog.git"
    empty.mkdir()
    code, out, _err = cfg_cli("--snapshot", "1.0.0", "--vanilla-repo", str(empty),
                              "--game-root", str(tmp_path / "game"))
    assert code == 0 and "Created tracker repo" in out
    assert (empty / "HEAD").is_file()


def test_a_snapshot_refuses_a_folder_that_holds_something_else(cfg_cli, tmp_path):
    used = tmp_path / "not-a-tracker"
    used.mkdir()
    (used / "notes.txt").write_text("mine")
    code, _out, err = cfg_cli("--snapshot", "1.0.0", "--vanilla-repo", str(used))
    assert code == 1 and "is not a tracker repository and is not empty" in err
    assert (used / "notes.txt").read_text() == "mine"
