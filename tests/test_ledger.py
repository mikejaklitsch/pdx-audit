"""Findings ledger: content fingerprints, dismissals, open-finding tracking,
and the triage split between this patch and earlier drift."""
from pdxaudit.report import Finding, render_triage
from pdxaudit import ledger


def _key(yours="cost = 1", vanilla="cost = 2", name="blk"):
    return {"target": f"override:cat/{name}", "path": [], "yours": yours, "vanilla": vanilla}


def _f(kind="override_vanilla_changed_mid", name="blk", loc="m.txt:1", detail="d",
       key=None, since=None, base=None):
    key = key if key is not None else _key(name=name)
    return Finding(kind, name, loc, detail, None, key, since, base)


# --- fingerprints -----------------------------------------------------------

def test_fingerprint_ignores_location_and_detail():
    a = _f(loc="m.txt:1", detail="x")
    b = _f(loc="other.txt:99", detail="y")
    assert ledger.finding_id(a) == ledger.finding_id(b)
    assert len(ledger.finding_id(a)) == 40


def test_fingerprint_changes_when_a_value_changes():
    a = _f()
    b = _f(key=_key(vanilla="cost = 3"))
    c = _f(key=_key(yours="cost = 5"))
    assert len({ledger.finding_id(x) for x in (a, b, c)}) == 3


def test_fingerprint_contains_no_line_numbers_or_hashes():
    f = _f()
    assert "m.txt:1" not in repr(ledger.fingerprint_payload(f))


# --- dismissals -------------------------------------------------------------

def test_dismiss_hides_and_counts():
    state = ledger.empty_state()
    f = _f()
    done, errors = ledger.dismiss(state, [f], [ledger.finding_id(f)[:8]], "on purpose", "2026-09-12")
    assert not errors and len(done) == 1
    entry = state["dismissed"][ledger.finding_id(f)]
    assert entry["reason"] == "on purpose" and entry["on"] == "2026-09-12"
    visible, hidden = ledger.split_dismissed([f, _f(name="other")], state)
    assert [x.name for x in visible] == ["other"] and hidden == 1


def test_dismissed_finding_returns_when_value_changes():
    state = ledger.empty_state()
    f = _f()
    ledger.dismiss(state, [f], [ledger.finding_id(f)], None, "2026-09-12")
    changed = _f(key=_key(vanilla="cost = 4"))
    visible, hidden = ledger.split_dismissed([changed], state)
    assert visible == [changed] and hidden == 0


def test_duplicates_cannot_be_dismissed():
    state = ledger.empty_state()
    f = _f(kind="dupes_multiple_sources", key={"target": "dupes:common/x/a"})
    done, errors = ledger.dismiss(state, [f], [ledger.finding_id(f)[:8]], None, "2026-09-12")
    assert not done and errors and "cannot be dismissed" in errors[0]
    assert state["dismissed"] == {}


def test_unknown_and_ambiguous_ids_are_errors():
    state = ledger.empty_state()
    fs = [_f(name=f"b{i}") for i in range(40)]
    _done, errors = ledger.dismiss(state, fs, ["zzzzzzzz"], None, "d")
    assert errors and "no current finding" in errors[0]
    ids = [ledger.finding_id(f) for f in fs]
    # find two ids sharing a first character to build an ambiguous prefix
    first = next(i[0] for i in ids if sum(j[0] == i[0] for j in ids) > 1)
    _done, errors = ledger.dismiss(state, fs, [first], None, "d")
    assert errors and "ambiguous" in errors[0]


def test_undismiss_by_prefix():
    state = ledger.empty_state()
    f = _f()
    ledger.dismiss(state, [f], [ledger.finding_id(f)], None, "d")
    removed, errors = ledger.undismiss(state, [ledger.finding_id(f)[:8]])
    assert removed and not errors and state["dismissed"] == {}


# --- open findings ----------------------------------------------------------

def test_open_findings_keep_first_seen_since_and_close_when_gone():
    state = ledger.empty_state()
    a = _f(name="a", since="1.3.8")
    b = _f(name="b", since="1.3.11")
    ledger.update_open(state, [a, b], "1.3.11")
    assert state["open"][ledger.finding_id(a)]["since"] == "1.3.8"
    later_a = _f(name="a", since="1.3.11")        # recomputed later with a newer since
    ledger.update_open(state, [later_a], "1.3.12")
    assert state["open"][ledger.finding_id(a)]["since"] == "1.3.8"
    assert ledger.finding_id(b) not in state["open"]   # fixed: closed


def test_open_findings_default_since_is_new_tag_and_info_is_skipped():
    state = ledger.empty_state()
    info = _f(kind="override_inject_context", name="i")
    act = _f(name="a")
    ledger.update_open(state, [info, act], "1.3.11")
    assert list(state["open"]) == [ledger.finding_id(act)]
    assert state["open"][ledger.finding_id(act)]["since"] == "1.3.11"


def test_bases_are_the_oldest_open_base_the_tracker_knows():
    state = ledger.empty_state()
    state["open"]["1"] = {"target": "loc:english/A", "base": "1.3.8"}
    state["open"]["2"] = {"target": "loc:english/A", "base": "1.3.4"}
    state["open"]["3"] = {"target": "loc:english/B", "base": "0.9"}
    order = ["1.2.0", "1.3.4", "1.3.8", "1.3.10", "1.3.11"]   # oldest first
    assert ledger.bases_from_state(state, order) == {"loc:english/A": "1.3.4"}


# --- triage rendering -------------------------------------------------------

def test_triage_splits_this_patch_from_earlier_and_shows_ids():
    now = _f(name="now_blk", since="1.3.11")
    old = _f(name="old_blk", since="1.3.8")
    out = render_triage([now, old], "1.3.10 Pavia", "1.3.11 Pavia", ["overrides"],
                        new_tag="1.3.11", dismissed=2)
    assert out.index("now_blk") < out.index("Still open from earlier patches") < out.index("old_blk")
    assert f"[{ledger.finding_id(now)[:8]}]" in out
    assert "(1.3.8)" in out
    assert "2 dismissed" in out and "--show-dismissed" in out


def test_triage_omits_ids_for_non_dismissible():
    dup = _f(kind="dupes_multiple_sources", name="toll_castle",
             key={"target": "dupes:common/building_types/toll_castle"})
    out = render_triage([dup], "", "1.3.11 Pavia", ["dupes"], new_tag="1.3.11")
    assert "toll_castle" in out
    assert f"[{ledger.finding_id(dup)[:8]}]" not in out
