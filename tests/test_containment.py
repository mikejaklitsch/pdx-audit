"""The containment check: which vanilla changes is the mod copy missing."""
from pdxaudit.gui import _containment


def test_added_line_present_in_mod_is_not_missing():
    old = "cost = 100\n"
    new = "cost = 100\nupkeep = 5\n"
    mod = "cost = 100\nupkeep = 5\nmaintenance = local\n"
    missing, kept = _containment(mod, old, new)
    assert missing == []      # mod already has upkeep = 5
    assert kept == []


def test_added_line_absent_from_mod_is_missing():
    old = "cost = 100\n"
    new = "cost = 100\nupkeep = 5\n"
    mod = "cost = 100\n"       # mod never picked up upkeep
    missing, kept = _containment(mod, old, new)
    assert any("upkeep = 5" in m for m in missing)


def test_removed_line_still_carried_is_kept():
    old = "cost = 100\nlegacy = 1\n"
    new = "cost = 100\n"        # vanilla dropped legacy
    mod = "cost = 100\nlegacy = 1\n"   # mod still carries it
    missing, kept = _containment(mod, old, new)
    assert any("legacy = 1" in k for k in kept)


def test_brace_formatting_is_normalized():
    # a formatter collapsing a block onto one line must not read as drift
    old = "a = {\n b = 1\n}\n"
    new = "a = { b = 1 }\n"
    missing, kept = _containment(new, old, new)
    assert missing == [] and kept == []
