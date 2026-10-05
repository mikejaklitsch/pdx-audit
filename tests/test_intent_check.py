"""The lint gate and the per-copy deviation cache."""
import json

from conftest import build_tracker, _write_tree
from pdxaudit import intent, intent_check
from pdxaudit.base import VanillaBase
from pdxaudit.ledger import empty_state
from pdxaudit.tracker import get_commits, tag_of

LAW = "in_game/common/laws/l.txt"
V1 = "law_a = {\n\tcost = 1\n\tx = 1\n}\n"
V2 = "law_a = {\n\tcost = 2\n\tx = 1\n}\n"
MOD = "REPLACE:law_a = {\n\tcost = 1\n\tx = 5\n}\n"


def _setup(tmp_path, versions=(("1.0", V1), ("1.1", V1))):
    tr = build_tracker(tmp_path, [(tag, {LAW: text}) for tag, text in versions])
    mod = tmp_path / "mod"
    _write_tree(mod, {".metadata/metadata.json": '{"id": "t"}', "in_game/common/laws/fe.txt": MOD})
    return tr, mod


def _collect(tr, mod, upto=None):
    commits = get_commits(tr.repo)
    if upto:
        commits = commits[[tag_of(m) for _h, m in commits].index(upto):]
    copies, devs = intent.run_collect(mod, VanillaBase(tr.repo), commits, *commits[0])
    order = [tag_of(m) for _h, m in reversed(commits)]
    return copies, devs, order


def test_baseline_exempts_old_deviations_and_flags_new_ones(tmp_path):
    tr, mod = _setup(tmp_path)
    copies, devs, order = _collect(tr, mod)
    state = empty_state()
    it = intent.of(state)
    assert intent_check.check(it, copies, devs, order)["findings"][0]["type"] == "no_baseline"
    assert intent_check.set_baseline(it, copies, devs, None, "1.1", "2026-10-02") == 1
    assert intent_check.check(it, copies, devs, order)["findings"] == []
    (mod / "in_game/common/laws/fe.txt").write_text(MOD.replace("x = 5", "x = 6\n\ty = 1"))
    copies, devs, order = _collect(tr, mod)
    res = intent_check.check(it, copies, devs, order)
    assert sorted(f["path"][-1] for f in res["findings"]) == ["x", "y"]
    assert all(f["type"] == "unattributed" for f in res["findings"])
    assert intent_check.lines(res)[0].startswith("in_game/common/laws/fe.txt:")


def test_a_patch_that_conflicts_brings_an_old_deviation_back(tmp_path):
    tr, mod = _setup(tmp_path, (("1.0", V1), ("1.1", V1), ("1.2", V2)))
    copies, devs, order = _collect(tr, mod, upto="1.1")
    it = intent.of(empty_state())
    intent_check.set_baseline(it, copies, devs, None, "1.1", "2026-10-02")
    copies, devs, order = _collect(tr, mod)
    res = intent_check.check(it, copies, devs, order)
    kinds = {(f["path"][-1], f["change"]) for f in res["findings"]}
    assert ("cost", "vanilla_changed") in kinds        # vanilla changed it after the baseline
    assert not any(p == "x" for p, _c in kinds)         # the old edit stays exempt


def test_rules_explain_and_stale_entries_are_findings(tmp_path):
    tr, mod = _setup(tmp_path)
    copies, devs, order = _collect(tr, mod)
    state = empty_state()
    it = intent.of(state)
    intent_check.set_baseline(it, copies, [], None, "1.1", "2026-10-02")   # nothing exempt
    intent.add_rule(state, {"id": "r", "system": "s", "reason": "x is ours", "source": {"kind": "user"},
                            "disposition": "keep_mod", "match": {"mod_key": ["x"]}})
    res = intent_check.check(it, copies, devs, order)
    assert res["findings"] == [] and res["attributed"] == 1
    d = next(d for d in devs if d.mod_key == "x")
    e = intent.entry_from(d, "e1", "s", "x is ours", {"kind": "user"}, "keep_mod")
    intent.seal(e, copies, devs)
    e["seen"]["mod_sig"] = "0" * 12                  # as if the mod changed since
    intent.add_entry(state, e)
    res = intent_check.check(it, copies, devs, order)
    assert [f["type"] for f in res["findings"]] == ["stale"]     # the rule still explains x
    intent.remove(state, "r")
    res = intent_check.check(it, copies, devs, order)
    assert [f["type"] for f in res["findings"]] == ["unattributed", "stale"]
    res = intent_check.check(it, copies, devs, order, changed={"in_game/other.txt"})
    assert res["findings"] == []


def test_the_deviation_cache_gives_the_same_deviations(tmp_path):
    tr, mod = _setup(tmp_path)
    copies, devs, _o = _collect(tr, mod)
    assert all(c.cached is None for c in copies)
    from pdxaudit.tracker import cache_dir_of
    cache = list(cache_dir_of(tr.repo).glob("devs-v1-*.json"))
    assert len(cache) == 1 and json.loads(cache[0].read_text())
    copies2, devs2, _o = _collect(tr, mod)
    assert copies2 and all(c.cached is not None for c in copies2)
    assert [(d.id, d.line, d.file) for d in devs2] == [(d.id, d.line, d.file) for d in devs]
    # a moved copy keeps its deviations and takes its new line
    (mod / "in_game/common/laws/fe.txt").write_text("\n\n" + MOD)
    _c, devs3, _o = _collect(tr, mod)
    assert [d.line for d in devs3] == [d.line + 2 for d in devs]


def test_the_pdx_lint_check_passes_changed_files_and_returns_lines(tmp_path, monkeypatch):
    import importlib.util
    import subprocess
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "port_intent", Path(__file__).resolve().parent.parent / "contrib" / "port_intent.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    seen = {}

    def fake(cmd, **_kw):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 1, json.dumps({"lines": ["a.txt:1: x"]}), "")
    monkeypatch.setattr(mod.subprocess, "run", fake)
    root = tmp_path / "mod"
    (root / "in_game").mkdir(parents=True)
    assert mod.run(root, {root / "in_game" / "a.txt", root / "in_game" / "b.yml"}) == ["a.txt:1: x"]
    assert seen["cmd"][-2:] == ["--changed", "in_game/a.txt"]
    assert mod.run(root, {root / "in_game" / "b.yml"}) == []
    assert mod.run(root) == ["a.txt:1: x"] and "--changed" not in seen["cmd"]
