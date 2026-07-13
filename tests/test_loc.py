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


def test_clean_when_nothing_drifted(monkeypatch):
    mod_text = 'l_english:\n KEY_A:0 "mine"\n'
    old = {("english", "KEY_A"): "V"}
    new = {("english", "KEY_A"): "V"}
    out = _run_loc(monkeypatch, mod_text, old, new)
    assert "All overridden localization keys are current with vanilla." in out
