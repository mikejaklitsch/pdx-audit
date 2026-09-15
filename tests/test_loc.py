"""Localization audit: parsing and the five classification branches."""
import io
import types
from contextlib import redirect_stdout

import pdxaudit.loc as loc


def test_parse_loc_reads_language_and_keys():
    text = ('l_english:\n'
            ' KEY_A:0 "hello"\n'
            ' KEY_B: "no version number"\n'
            ' # a comment\n'
            '\n')
    parsed = loc.parse_loc(text)
    assert parsed[("english", "KEY_A")] == "hello"
    assert parsed[("english", "KEY_B")] == "no version number"


def test_parse_loc_keys_are_language_scoped():
    text = 'l_french:\n KEY_A:0 "bonjour"\n'
    parsed = loc.parse_loc(text)
    assert ("french", "KEY_A") in parsed
    assert ("english", "KEY_A") not in parsed


def _run_loc(monkeypatch, mod_text, old, new):
    monkeypatch.setattr(loc, "mod_loc_files",
                        lambda mr: [("main_menu/localization/english/t_l_english.yml", mod_text)])

    def fake_build(repo, commit, wanted, label=""):
        d = old if commit == "OLD" else new
        return {k: v for k, v in d.items() if k in wanted}
    monkeypatch.setattr(loc, "build_loc_vanilla", fake_build)
    args = types.SimpleNamespace(block=None)
    buf = io.StringIO()
    with redirect_stdout(buf):
        loc.run_loc_audit("/mod", "repo", "OLD", "1.2 Old", "NEW", "1.3 New", args)
    return buf.getvalue()


def test_all_five_branches(monkeypatch):
    mod_text = ('l_english:\n'
                ' KEY_CHANGED:0 "mine"\n'
                ' KEY_REMOVED:0 "mine"\n'
                ' KEY_COLLISION:0 "mine"\n'
                ' KEY_UNCHANGED:0 "same"\n'
                ' KEY_MODONLY:0 "mine"\n')
    E = "english"
    old = {(E, "KEY_CHANGED"): "A", (E, "KEY_REMOVED"): "gone", (E, "KEY_UNCHANGED"): "same"}
    new = {(E, "KEY_CHANGED"): "B", (E, "KEY_COLLISION"): "vnew", (E, "KEY_UNCHANGED"): "same"}
    out = _run_loc(monkeypatch, mod_text, old, new)

    assert "**1** vanilla changed the string" in out
    assert "**1** vanilla removed the key" in out
    assert "**1** vanilla newly added" in out
    assert "**1** unchanged, **1** mod-only" in out
    # changed section shows both vanilla values
    assert '"A"' in out and '"B"' in out
    assert "KEY_REMOVED" in out and "Keys Removed from Vanilla" in out
    assert "KEY_COLLISION" in out and "New Name Collisions" in out
    assert "KEY_UNCHANGED" not in out   # unchanged keys show only in the summary tally


def _run_loc_ctx(monkeypatch, mod_text, values_by_commit, ctx):
    monkeypatch.setattr(loc, "mod_loc_files",
                        lambda mr: [("main_menu/localization/english/t_l_english.yml", mod_text)])

    def fake_build(repo, commit, wanted, label=""):
        return {k: v for k, v in values_by_commit[commit].items() if k in wanted}
    monkeypatch.setattr(loc, "build_loc_vanilla", fake_build)
    args = types.SimpleNamespace(block=None)
    buf = io.StringIO()
    with redirect_stdout(buf):
        findings = loc.run_loc_audit("/mod", "repo", "OLD", "1.2 Old", "NEW", "1.3 New", args, ctx)
    return findings, buf.getvalue()


def _ctx(bases=None, fixed=False):
    return types.SimpleNamespace(
        commits=[("NEW", "1.3 New"), ("OLD", "1.2 Old"), ("OLDER", "1.1 Older")],
        new_tag="1.3", fixed_window=fixed, bases=bases or {}, scanned={})


def test_findings_carry_keys_and_since(monkeypatch):
    E = "english"
    values = {"OLDER": {(E, "KEY_A"): "A"}, "OLD": {(E, "KEY_A"): "A"},
              "NEW": {(E, "KEY_A"): "B"}}
    findings, _ = _run_loc_ctx(monkeypatch, 'l_english:\n KEY_A:0 "mine"\n', values, _ctx())
    f = next(f for f in findings if f.kind == "loc_changed")
    assert f.key["target"] == "loc:english/KEY_A"
    assert f.since == "1.3" and f.base == "1.2"


def test_open_finding_base_carries_a_change_forward(monkeypatch):
    E = "english"
    values = {"OLDER": {(E, "KEY_A"): "A"}, "OLD": {(E, "KEY_A"): "B"},
              "NEW": {(E, "KEY_A"): "B"}}
    mod = 'l_english:\n KEY_A:0 "mine"\n'
    findings, _ = _run_loc_ctx(monkeypatch, mod, values, _ctx())
    assert not [f for f in findings if f.kind == "loc_changed"]      # window saw no change
    findings, out = _run_loc_ctx(monkeypatch, mod, values,
                                 _ctx(bases={"loc:english/KEY_A": "1.1"}))
    f = next(f for f in findings if f.kind == "loc_changed")
    assert f.since == "1.2" and f.base == "1.1"
    assert '"A"' in out and '"B"' in out


def test_loc_entries_keep_repeats_and_lines():
    text = 'l_english:\n KEY:0 "one"\n\n KEY:0 "two"\n'
    assert list(loc.loc_entries(text)) == [("english", "KEY", "one", 2), ("english", "KEY", "two", 4)]
    assert loc.parse_loc(text) == {("english", "KEY"): "two"}


def test_replace_folder_is_recognized():
    assert loc.is_replace_loc("main_menu/localization/english/replace/a_l_english.yml")
    assert loc.is_replace_loc("main_menu/localization/replace/english/a_l_english.yml")
    assert not loc.is_replace_loc("main_menu/localization/english/a_l_english.yml")
    assert not loc.is_replace_loc("main_menu/localization/english/replace_l_english.yml")


def test_loc_audit_takes_the_replace_copy_over_a_normal_one(monkeypatch):
    E = "english"
    monkeypatch.setattr(loc, "mod_loc_files", lambda mr: [
        ("main_menu/localization/english/a_l_english.yml", 'l_english:\n KEY_A:0 "normal"\n'),
        ("main_menu/localization/english/replace/b_l_english.yml", 'l_english:\n KEY_A:0 "replaced"\n')])
    values = {"OLD": {(E, "KEY_A"): "A"}, "NEW": {(E, "KEY_A"): "B"}}
    monkeypatch.setattr(loc, "build_loc_vanilla",
                        lambda repo, commit, wanted, label="": dict(values[commit]))
    with redirect_stdout(io.StringIO()):
        findings = loc.run_loc_audit("/mod", "repo", "OLD", "1.2 Old", "NEW", "1.3 New",
                                     types.SimpleNamespace(block=None))
    f = next(f for f in findings if f.kind == "loc_changed")
    assert f.key["mod"] == "replaced"
    assert f.location == "main_menu/localization/english/replace/b_l_english.yml"


def test_clean_when_nothing_drifted(monkeypatch):
    mod_text = 'l_english:\n KEY_A:0 "mine"\n'
    old = {("english", "KEY_A"): "V"}
    new = {("english", "KEY_A"): "V"}
    out = _run_loc(monkeypatch, mod_text, old, new)
    assert "All overridden localization keys are current with vanilla." in out
