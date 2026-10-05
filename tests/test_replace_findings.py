"""The `replace_findings` setting: by default the changes vanilla made in one REPLACE
are one finding, and `statement` makes each change a finding of its own. --set and
the app's Settings page write the same setting."""
import io
from contextlib import redirect_stdout

import pytest

from conftest import audit_args, make_ctx
from pdxaudit import config
from pdxaudit.overrides import run_override_audit

M_TXT = "in_game/common/building_types/m.txt"


def _run(world):
    with redirect_stdout(io.StringIO()) as buf:
        findings = run_override_audit(world.mod, world.repo, world.old, "1.0.0 Test", world.new,
                                      "1.1.0 Test", audit_args(), make_ctx(world.repo, "1.1.0"))
    return findings, buf.getvalue()


def test_the_changes_of_one_replace_are_one_finding(world, block_findings):
    findings, out = _run(world)
    assert [(f.kind, f.location, f.detail) for f in findings] == [
        ("override_block_changed_mid", f"{M_TXT}:2", "2 changes")]
    assert sorted(c[0] for c in findings[0].key["changes"]) == [
        "vanilla_added_mid", "vanilla_removed_mid"]
    assert out.count(f"`{M_TXT}:2`") == 1           # one id, with both changes under it
    assert "added upkeep = 5" in out and "legacy_mod = 1" in out


def test_your_edit_makes_the_block_finding_high(world, block_findings):
    (world.mod / M_TXT).write_text("REPLACE:some_building = {\n\tcost = 150\n\tlegacy_mod = 1\n}\n")
    findings, _out = _run(world)
    assert [f.kind for f in findings] == ["override_block_changed_high"]


def test_one_change_keeps_its_own_kind(world, block_findings):
    (world.mod / M_TXT).write_text("REPLACE:some_building = {\n\tcost = 100\n}\n")
    findings, _out = _run(world)
    assert [(f.kind, f.key["vanilla"]) for f in findings] == [("override_vanilla_added_mid", "upkeep = 5")]


def test_statement_makes_each_change_a_finding(world, statement_findings):
    findings, _out = _run(world)
    assert sorted(f.kind for f in findings) == ["override_vanilla_added_mid", "override_vanilla_removed_mid"]


def test_block_is_the_default(world, monkeypatch):
    monkeypatch.delenv("PDX_REPLACE_FINDINGS", raising=False)
    monkeypatch.setattr(config, "cfg", lambda key, default=None: default)
    findings, _out = _run(world)
    assert [f.kind for f in findings] == ["override_block_changed_mid"]


@pytest.fixture
def cfg_file(tmp_path, monkeypatch):
    monkeypatch.delenv("PDX_REPLACE_FINDINGS", raising=False)
    data = tmp_path / "data.json"
    monkeypatch.setattr(config, "writable_path", lambda: data)
    config.invalidate()
    yield data
    config.invalidate()


def test_set_stores_a_choice_and_refuses_others(cfg_file):
    config.set_value("replace_findings", "Statement")
    assert config.setting("replace_findings")[0] == "statement"
    with pytest.raises(config.ConfigError, match="block, statement"):
        config.set_value("replace_findings", "file")


def test_set_reads_a_folder_that_holds_git_as_its_git_folder(cfg_file, tmp_path):
    install = tmp_path / "install"
    (install / ".git" / "objects").mkdir(parents=True)
    (install / ".git" / "HEAD").write_text("ref: refs/heads/master\n")
    config.set_value("vanilla_repo", str(install))
    assert config.setting("vanilla_repo")[0] == str(install / ".git")
