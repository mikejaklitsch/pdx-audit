"""GUI audit end to end with adoption-based fork points: a reindented copy with
its own lines is not flagged, drift is split into this patch versus earlier
patches, a recorded review version wins, and partial adoption is reviewed."""
import io
from contextlib import redirect_stdout

from conftest import build_tracker, make_ctx, audit_args, _write_tree
from pdxaudit.gui import run_gui_audit

V10 = "template tip = {\n\ta = 1\n\tb = 2\n}\n"
V11 = "template tip = {\n\ta = 1\n\tb = 2\n\tc = 3\n\td = 4\n\te = 5\n}\n"
V12 = "template tip = {\n\ta = 1\n\tb = 2\n\tc = 3\n\td = 4\n\te = 5\n\tf = 6\n\tg = 7\n\th = 8\n}\n"
MOD_V11 = ("template tip = {\n    a = 1\n    b = 2\n    c = 3\n    d = 4\n    e = 5\n"
           "    mine = yes\n}\n")
MOD_V10 = "template tip = {\n    a = 1\n    b = 2\n    mine = yes\n}\n"


def _snap(tip, other="template other = {\n\tx = 1\n}\n"):
    return {"in_game/gui/tips.gui": tip, "in_game/gui/other.gui": other}


def _mod(tmp_path, tip_copy):
    mod = tmp_path / "mod"
    _write_tree(mod, {".metadata/metadata.json": '{"id": "t"}',
                      "in_game/gui/aaa_mod.gui": tip_copy})
    return mod


def _run(tr, mod, old, new, bases=None, fixed=False):
    ctx = make_ctx(tr.repo, new, fixed=fixed, bases=bases)
    with redirect_stdout(io.StringIO()):
        return run_gui_audit(mod, tr.repo, tr.hashes[old], f"{old} Test",
                             tr.hashes[new], f"{new} Test", audit_args(), ctx)


def _kinds(findings, name="tip"):
    return {(f.kind, f.since) for f in findings if f.name == name}


def test_reindented_copy_with_own_lines_is_current(tmp_path):
    tr = build_tracker(tmp_path, [("1.0", _snap(V10)), ("1.1", _snap(V11)),
                                  ("1.2", _snap(V11, "template other = {\n\tx = 2\n}\n"))])
    findings = _run(tr, _mod(tmp_path, MOD_V11), "1.1", "1.2")
    assert not [k for k in _kinds(findings) if k[0] in ("gui_shadow_stale", "gui_shadow_behind")]


def test_change_in_this_patch_is_stale(tmp_path):
    tr = build_tracker(tmp_path, [("1.0", _snap(V10)), ("1.1", _snap(V11)), ("1.2", _snap(V12))])
    findings = _run(tr, _mod(tmp_path, MOD_V11), "1.1", "1.2")
    assert ("gui_shadow_stale", "1.2") in _kinds(findings)


def test_change_from_earlier_patch_is_behind(tmp_path):
    tr = build_tracker(tmp_path, [("1.0", _snap(V10)), ("1.1", _snap(V11)), ("1.2", _snap(V12)),
                                  ("1.3", _snap(V12, "template other = {\n\tx = 2\n}\n"))])
    findings = _run(tr, _mod(tmp_path, MOD_V11), "1.2", "1.3")
    assert ("gui_shadow_behind", "1.2") in _kinds(findings)
    f = next(f for f in findings if f.kind == "gui_shadow_behind")
    assert f.base == "1.1"


def test_recorded_review_version_wins_over_detection(tmp_path):
    tr = build_tracker(tmp_path, [("1.0", _snap(V10)), ("1.1", _snap(V11)),
                                  ("1.2", _snap(V11, "template other = {\n\tx = 2\n}\n"))])
    mod = _mod(tmp_path, MOD_V10)
    assert not [k for k in _kinds(_run(tr, mod, "1.1", "1.2")) if k[0].startswith("gui_shadow")]
    findings = _run(tr, mod, "1.1", "1.2", bases={"gui:in_game/template/tip": "1.0"})
    assert ("gui_shadow_behind", "1.1") in _kinds(findings)


def test_partly_adopted_patch_is_reviewed(tmp_path):
    big = "template tip = {\n" + "".join(f"\tk{i} = {i}\n" for i in range(10)) + "}\n"
    part = "template tip = {\n" + "".join(f"    k{i} = {i}\n" for i in range(6)) + "}\n"
    tr = build_tracker(tmp_path, [("1.0", _snap("template tip = {\n}\n")), ("1.1", _snap(big)),
                                  ("1.2", _snap(big, "template other = {\n\tx = 2\n}\n"))])
    findings = _run(tr, _mod(tmp_path, part), "1.1", "1.2")
    hit = [f for f in findings if f.kind == "gui_partial_adoption" and f.name == "tip"]
    assert hit and "6/10" in hit[0].detail and "1.1" in hit[0].detail


def test_partly_adopted_patch_shows_the_lines_the_copy_lacks(tmp_path):
    big = "template tip = {\n" + "".join(f"\tk{i} = {i}\n" for i in range(10)) + "}\n"
    part = "template tip = {\n" + "".join(f"    k{i} = {i}\n" for i in range(6)) + "}\n"
    tr = build_tracker(tmp_path, [("1.0", _snap("template tip = {\n}\n")), ("1.1", _snap(big)),
                                  ("1.2", _snap(big, "template other = {\n\tx = 2\n}\n"))])
    ctx = make_ctx(tr.repo, "1.2")
    with redirect_stdout(io.StringIO()):
        findings = run_gui_audit(_mod(tmp_path, part), tr.repo, tr.hashes["1.1"], "1.1 Test",
                                 tr.hashes["1.2"], "1.2 Test", audit_args(results_file="r.json"), ctx)
    hit = next(f for f in findings if f.kind == "gui_partial_adoption" and f.name == "tip")
    patch = hit.data["patch"]
    assert [p["t"] for p in patch if p["c"] == "context"][:2] == ["template tip = {", "    k0 = 0"]
    assert [(p["t"], p["c"]) for p in patch if p["c"] != "context"] == [
        (f"    k{i} = {i}", "added") for i in range(6, 10)]


def test_findings_are_stable_when_their_own_open_bases_feed_the_next_run(tmp_path):
    from pdxaudit import ledger
    big = "template tip = {\n" + "".join(f"\tk{i} = {i}\n" for i in range(10)) + "}\n"
    part = "template tip = {\n" + "".join(f"    k{i} = {i}\n" for i in range(6)) + "}\n"
    tr = build_tracker(tmp_path, [("1.0", _snap("template tip = {\n}\n")), ("1.1", _snap(big)),
                                  ("1.2", _snap(big, "template other = {\n\tx = 2\n}\n"))])
    mod = _mod(tmp_path, part)
    first = _run(tr, mod, "1.1", "1.2")
    state = ledger.empty_state()
    ledger.update_open(state, first, "1.2")
    bases = ledger.bases_from_state(state, ["1.0", "1.1", "1.2"])
    second = _run(tr, mod, "1.1", "1.2", bases=bases)
    actionable = lambda fs: sorted(ledger.finding_id(f) for f in fs if ledger.is_actionable(f))
    assert actionable(first) and actionable(first) == actionable(second)


def test_vanilla_only_relaying_out_a_definition_is_not_drift(tmp_path):
    spread = 'template tip = {\n\tcolor = {\n\t\t0.0\n\t\t1.0\n\t}\n\ttext = "X"\n}\n'
    packed = 'template tip = {\n\tcolor = { 0.0 1.0 }\n\ttext = "X"\n}\n'
    tr = build_tracker(tmp_path, [("1.0", _snap(spread)), ("1.1", _snap(spread)), ("1.2", _snap(packed))])
    findings = _run(tr, _mod(tmp_path, spread), "1.1", "1.2")
    assert not _kinds(findings)


def test_findings_carry_fingerprint_keys(tmp_path):
    tr = build_tracker(tmp_path, [("1.0", _snap(V10)), ("1.1", _snap(V11)), ("1.2", _snap(V12))])
    findings = _run(tr, _mod(tmp_path, MOD_V11), "1.1", "1.2")
    f = next(f for f in findings if f.kind == "gui_shadow_stale")
    assert f.key["target"] == "gui:in_game/template/tip"
