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


def test_fingerprint_ignores_vanillas_text_before_a_conflict():
    assert ledger.finding_id(_f(key=dict(_key(), was="cost = 3"))) == ledger.finding_id(_f())


def test_fingerprint_contains_no_line_numbers_or_hashes():
    f = _f()
    assert "m.txt:1" not in repr(ledger.fingerprint_payload(f))


def test_findings_with_the_same_content_add_their_file_then_their_order_in_it():
    lone = _f(key=_key(yours="cost = 7"))
    found = [_f(loc="m.txt:9"), _f(loc="m.txt:3"), _f(loc="other.txt:1"), lone]
    out = ledger.distinct(found)
    assert out[3] == lone
    assert [f.key.get("file") for f in out[:3]] == ["m.txt", "m.txt", "other.txt"]
    assert [f.key.get("occurrence") for f in out[:3]] == [2, 1, None]
    assert len({ledger.finding_id(f) for f in out}) == 4
    assert ledger.distinct(out) == out


ROW = "\trow = {\n\t\tsize = %s\n\t}\n"
OLD_ROWS = "template foo = {\n" + ROW % 1 + ROW % 1 + "}\n"
NEW_ROWS = "template foo = {\n" + ROW % 2 + ROW % 2 + "}\n"


def _row_ids(text):
    from pdxaudit import changes
    result = changes.audit("gui", "foo", "gui:in_game/template/foo", text, [OLD_ROWS, NEW_ROWS], ["1.0", "1.1"],
                           "in_game/gui/a.gui", 1)
    return [ledger.finding_id(f) for f in result.findings]


def test_identical_blocks_keep_their_ids_through_an_unrelated_edit():
    same = _row_ids(OLD_ROWS)
    moved = _row_ids("template foo = {\n\tother = { x = 1 }\n" + ROW % 1 + ROW % 1 + "}\n")
    assert len(set(same)) == 2 and moved == same


def test_editing_one_of_two_identical_blocks_hands_neither_its_old_id():
    same = _row_ids(OLD_ROWS)
    changed = _row_ids("template foo = {\n" + ROW % 3 + ROW % 1 + "}\n")
    assert len(changed) == 2 and not set(same) & set(changed)


def test_copies_in_two_files_add_their_file_and_their_rows_follow():
    from pdxaudit import changes
    old, new = "template foo = {\n\tsize = { 10 10 }\n}\n", "template foo = {\n\tsize = { 20 20 }\n}\n"

    def copy(file):
        return changes.audit("gui", "foo", "gui:in_game/template/foo", old, [old, new], ["1.0", "1.1"],
                             file, 1, want=True)

    lone = copy("in_game/gui/a.gui")
    fixed = changes.distinct([lone, copy("in_game/gui/b.gui")])
    assert "file" not in lone.findings[0].key
    assert [r.findings[0].key["file"] for r in fixed] == ["in_game/gui/a.gui", "in_game/gui/b.gui"]
    ids = [ledger.finding_id(r.findings[0]) for r in fixed]
    assert len(set(ids)) == 2 and ledger.finding_id(lone.findings[0]) not in ids
    for r, fid in zip(fixed, ids):
        assert {ledger.finding_id(f) for _c, f in r.flagged} == {fid}
        assert {c["fid"] for c in r.block["changes"] if c["fid"]} == {fid}


def test_only_window_checks_carry_their_base_forward():
    state = ledger.empty_state()
    state["open"] = {
        "a": {"finding": "override_vanilla_added_high", "target": "override:cat/b", "base": "1.0"},
        "b": {"finding": "override_inject_overlap", "target": "override:cat/b", "base": "1.1"},
        "c": {"finding": "override_vanilla_added_high", "target": "override:cat/c", "base": "1.0"},
        "d": {"finding": "loc_changed", "target": "loc:english/KEY", "base": "1.0"}}
    assert ledger.bases_from_state(state, ["1.0", "1.1", "1.2"]) == {
        "override:cat/b": "1.1", "loc:english/KEY": "1.0"}


def test_a_run_of_some_audits_keeps_the_other_audits_open_findings():
    state = ledger.empty_state()
    gui = _f(kind="gui_vanilla_changed_mid", key=dict(_key(), target="gui:in_game/template/w"))
    ov = _f()
    ledger.update_open(state, [gui, ov], "1.1")
    ledger.update_open(state, [], "1.1", covers=lambda e: ledger.audit_of(e) == "gui")
    assert list(state["open"]) == [ledger.finding_id(ov)]


def test_a_dismissal_a_run_did_not_produce_is_marked_gone():
    state = ledger.empty_state()
    f = _f()
    fid = ledger.finding_id(f)
    ledger.dismiss(state, [f], [fid], None, "2026-09-14")
    ledger.update_open(state, [], "1.1", produced=set())
    assert state["dismissed"][fid]["gone"]
    ledger.update_open(state, [], "1.1", produced={fid})
    assert "gone" not in state["dismissed"][fid]


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
    state["open"]["1"] = {"finding": "loc_changed", "target": "loc:english/A", "base": "1.3.8"}
    state["open"]["2"] = {"finding": "loc_changed", "target": "loc:english/A", "base": "1.3.4"}
    state["open"]["3"] = {"finding": "loc_changed", "target": "loc:english/B", "base": "0.9"}
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
