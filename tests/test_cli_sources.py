"""The source options on the command line: each writes the same stored files as the
function the app calls, a run with no chosen sources prints what it printed before,
and notes about sources go to stderr only."""
import io
import json
import re
import sys
from contextlib import redirect_stderr, redirect_stdout

import pytest

import pdxaudit.config as config
from conftest import _write_tree
from pdxaudit import sources as S
from pdxaudit.cli import build_parser, main
from test_sources import folder_source, git_source, meta


@pytest.fixture
def cli(world, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("PDX_GAME_ROOT", str(tmp_path / "no-game"))
    monkeypatch.setattr(config, "_CACHE", {})

    def run(*argv):
        monkeypatch.setattr(sys, "argv", ["pdx-audit", "--mod-root", str(world.mod), "--vanilla-repo",
                                          world.repo, "--color", "never", *argv])
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with redirect_stdout(out), redirect_stderr(err):
            try:
                main()
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        return code, out.getvalue(), err.getvalue()

    run.world, run.tmp = world, tmp_path
    return run


def _stored(data):
    """Every stored sources file, with storage keys' random suffixes blanked."""
    out = {}
    for fp in sorted(data.rglob("*.json")):
        rel = fp.relative_to(data).as_posix()
        if rel.startswith("sources/cache/") or rel.endswith("results.json") or rel == "sources/scan.json":
            continue
        out[re.sub(r"-[0-9a-f]{6}", "-KEY", rel)] = re.sub(r"-[0-9a-f]{6}\b", "-KEY", fp.read_text())
    return out


def test_each_option_writes_what_its_app_action_writes(cli, monkeypatch):
    world, tmp = cli.world, cli.tmp
    found = git_source(tmp / "found", [("1.0", {}), ("1.1", {})])
    other = git_source(tmp / "other", [("3.0", {})], mod_id="other")
    fold = folder_source(tmp / "fold")
    steps = [
        (["--add-source", str(found), "--as", "foundation"],
         lambda: S.add_source(world.mod, found, "foundation", vanilla_repo=world.repo)),
        (["--add-source", str(fold), "--as", "foundation", "--kind", "folder"],
         lambda: S.add_source(world.mod, fold, "foundation", "folder", vanilla_repo=world.repo)),
        (["--add-source", str(other), "--as", "adopted"],
         lambda: S.add_source(world.mod, other, "adopted", vanilla_repo=world.repo)),
        (["--move-source", "fold", "1"], lambda: S.move_source(world.mod, "fold", 1)),
        (["--set-kind", "found", "folder"], lambda: S.set_kind(world.mod, "found", "folder")),
        (["--set-kind", "found", "auto"], lambda: S.set_kind(world.mod, "found", "auto")),
        (["--rename", "other", "old_", "new_"], lambda: S.add_rename(world.mod, "other", "old_", "new_")),
        (["--unrename", "other", "old_"], lambda: S.remove_rename(world.mod, "other", "old_")),
        (["--patch", "found", "1.0..1.1", "1.0.0"],
         lambda: S.assign_patch(world.mod, "found", "1.0..1.1", "1.0.0", world.repo)),
        (["--snapshot-source", "fold"], lambda: S.snapshot_source(world.mod, "fold", world.repo)),
        (["--ignore-suggestion", "some_dep"], lambda: S.ignore_suggestion(world.mod, "some_dep")),
        (["--remove-source", "other"], lambda: S.remove_source(world.mod, "other")),
    ]
    by_cli, by_app = tmp / "by-cli", tmp / "by-app"
    for argv, action in steps:
        monkeypatch.setenv("XDG_DATA_HOME", str(by_cli))
        code, _out, err = cli(*argv)
        assert code == 0, (argv, err)
        monkeypatch.setenv("XDG_DATA_HOME", str(by_app))
        action()
        assert _stored(by_cli / "pdx-audit") == _stored(by_app / "pdx-audit"), argv


def test_a_change_on_the_command_line_is_read_fresh(cli):
    found = git_source(cli.tmp / "found", [("1.0", {})])
    before = S.load_sources(cli.world.mod)
    assert before.foundations == []
    assert cli("--add-source", str(found), "--as", "foundation")[0] == 0
    assert [s.id for s in S.load_sources(cli.world.mod).foundations] == ["found"]


def test_errors_and_argument_rules(cli):
    code, _out, err = cli("--remove-source", "nothing")
    assert code == 1 and "not a chosen source" in err
    assert cli("--add-source", str(cli.tmp))[0] == 2
    assert cli("--kind", "git")[0] == 2
    assert cli("--sources", "--remove-source", "x")[0] == 2
    assert cli("--vanilla-only", "--adopted", "x")[0] == 2
    code, _out, err = cli("--force")
    assert code == 2 and "--remove-orphaned-sources" in err


def test_sources_lists_choices_versions_and_suggestions(cli):
    found = git_source(cli.tmp / "found", [("1.0", {}), ("1.1", {})])
    cli("--add-source", str(found), "--as", "foundation")
    code, out, _err = cli("--sources")
    assert code == 0
    assert "## Foundations, in load order" in out and "**found** (git)" in out
    assert "1.0 (no patch), 1.1 (1.1.0, defaulted)" in out
    assert "1 versions have no patch" in out and "--patch found" in out


def test_remove_orphaned_sources_through_the_command_line(cli):
    found = git_source(cli.tmp / "found", [("1.0", {})])
    cli("--add-source", str(found), "--as", "foundation")
    cli("--remove-source", "found")
    code, _out, err = cli("--overrides")
    assert "chosen by no mod" in err
    code, out, _err = cli("--remove-orphaned-sources", "--force")
    assert code == 0 and "Removed 1 source(s)." in out and S.registry() == {}


def test_a_declared_dependency_changes_nothing_but_a_stderr_note(cli, tmp_path):
    results = tmp_path / "r.json"
    code, before, _err = cli("--results-file", str(results))
    payload_before = json.loads(results.read_text())
    _write_tree(cli.world.mod, {".metadata/metadata.json": json.dumps(
        {"name": "Test Mod", "id": "testmod", "relationships": [{"rel_type": "dependency", "id": "found"}]})})
    code, after, err = cli("--results-file", str(results))
    payload_after = json.loads(results.read_text())
    assert after == before
    for p in (payload_before, payload_after):
        p.pop("files")
        p.pop("warnings")
    assert payload_after == payload_before
    assert "run `pdx-audit --sources`" in err


def test_a_source_whose_folder_is_gone_is_warned_and_left_out(cli, tmp_path):
    found = git_source(cli.tmp / "found", [("1.0", {})])
    cli("--add-source", str(found), "--as", "foundation")
    found.rename(cli.tmp / "gone")
    results = tmp_path / "r.json"
    code, _out, err = cli("--results-file", str(results))
    assert "no longer exists" in err
    assert any("--relocate-source found" in w for w in json.loads(results.read_text())["warnings"])


def test_source_version_rules(cli):
    cli("--add-source", str(git_source(cli.tmp / "found", [("1.0", {})])), "--as", "foundation")
    assert cli("--vanilla-only", "--source-version", "found", "1.0")[0] == 2
    assert cli("--source-version", "nothing", "1.0")[0] == 1


def test_source_options_sit_in_their_own_help_group():
    text = build_parser().format_help()
    main_part, _sep, group = text.partition("\nsources:\n")
    assert "--add-source" in group and "--add-source" not in main_part
