"""The plan holds the result of each choice, and `build` makes a file from the plan
and the user's choices. The tests prove that this gives the merge that a run of the
merge with the choices in its decide callback gives: for each merge of the merge
tests, and for many sets of choices. They prove too that the dry run with choices,
`build` on a plan without choices, and --apply with choices give one file."""
import inspect
import itertools
import json
import random

import pytest

from conftest import build_tracker, _write_tree
from pdxaudit import merge, merge_cli

ID = "x/copy"


def _cases(monkeypatch):
    """[(kind, args, kwargs)] for each merge that the merge tests run."""
    import test_merge
    calls = []
    real_ops, real_inject = merge.merge_ops, merge.inject_ops

    def ops(*args, **kwargs):
        calls.append(("merge", args, kwargs))
        return real_ops(*args, **kwargs)

    def inject(*args, **kwargs):
        calls.append(("inject", args, kwargs))
        return real_inject(*args, **kwargs)
    monkeypatch.setattr(merge, "merge_ops", ops)
    monkeypatch.setattr(merge, "inject_ops", inject)
    for name, test in inspect.getmembers(test_merge, inspect.isfunction):
        if name.startswith("test_") and not inspect.signature(test).parameters:
            test()
    monkeypatch.setattr(merge, "merge_ops", real_ops)
    monkeypatch.setattr(merge, "inject_ops", real_inject)
    return calls


def _run(kind, args, kwargs, decide):
    """(decisions, ops) of one merge with `decide`, each decision with its address."""
    args = list(args)
    if kind == "merge":
        names = list(inspect.signature(merge.merge_ops).parameters)
        call = dict(zip(names, args), **kwargs)
        call["decide"] = decide
        decisions, ops = merge.merge_ops(**call)
    else:
        decisions, ops = merge.inject_ops(*args[:3], decide)
    for d in decisions:
        d.address = {"identity": ID, "path": d.path}
    return decisions, ops


def _default(kind, args, kwargs):
    if kind == "merge":
        names = list(inspect.signature(merge.merge_ops).parameters)
        return dict(zip(names, args), **kwargs).get("decide")
    return args[3] if len(args) > 3 else kwargs.get("decide")


def _direct(kind, args, kwargs, rec):
    """The merge with the choices in its decide callback, as the plan made it before it
    held the result of each choice."""
    default = _default(kind, args, kwargs) or (
        (lambda path, k, o, t, tt, what: (merge.OPEN, None)) if kind == "inject" else
        (lambda path, k, o, t, tt, what: (merge.OPEN, None) if what == "conflict" else (merge.TAKE, None)))

    def decide(path, k, o, t, tt, what):
        k = "inject_overlap" if kind == "inject" else k
        choice = rec["nodes"].get(merge_cli._key(ID, path, k)) or rec.get("all")
        return (choice, "choice") if choice else default(path, k, o, t, tt, what)
    decisions, ops = _run(kind, args, kwargs, decide)
    ours = args[1]
    return merge.finish(ours, ops, decisions, inject=kind == "inject")


def _built(kind, args, kwargs, rec):
    """The merge that build makes from the gathered parts and the choices."""
    decisions, ops = _run(kind, args, kwargs, _default(kind, args, kwargs))
    took, all_ops = _run(kind, args, kwargs, merge_cli._take_all)
    rows, op_rows = merge_cli.copy_parts(decisions, ops, took, all_ops, {})
    ours = args[1]
    cp = {"start": 0, "size": len(ours), "text": None, "inject": kind == "inject", "ops": op_rows,
          "decisions": rows}
    chosen = [merge_cli._chosen(r, rec, []) for r in rows]
    return merge_cli._copy_merge(ours, cp, chosen, None, None), [d for d, _s in chosen]


def _choice_sets(keys, rng):
    """Each set of choices for a few decisions; for more, a sample, and all take, all keep."""
    out = [{"all": a, "nodes": {}} for a in (None, merge.TAKE, merge.KEEP)]
    options = (None, merge.TAKE, merge.KEEP)
    combos = itertools.product(options, repeat=len(keys)) if len(keys) <= 4 else (
        [rng.choice(options) for _k in keys] for _n in range(60))
    for combo in combos:
        out.append({"all": None, "nodes": {k: c for k, c in zip(keys, combo) if c}})
    out.append({"all": merge.KEEP, "nodes": {k: merge.TAKE for k in keys[::2]}})
    return out


def test_build_gives_the_merge_that_decides_each_choice(monkeypatch):
    rng = random.Random(7)
    cases = _cases(monkeypatch)
    assert len(cases) > 40
    compared = 0
    for kind, args, kwargs in cases:
        decisions, _ops = _run(kind, args, kwargs, _default(kind, args, kwargs))
        keys = sorted({merge_cli.choice_key(d.to_json()) for d in decisions})
        for rec in _choice_sets(keys, rng):
            want = _direct(kind, args, kwargs, rec)
            got, got_decisions = _built(kind, args, kwargs, rec)
            assert got.text == want.text
            assert got.unexplained == want.unexplained
            assert [(op.start, op.end, op.text) for op in got.ops] == [(op.start, op.end, op.text) for op in want.ops]
            assert got_decisions == [d.to_json() for d in want.decisions]
            compared += 1
    assert compared > 500


# --- the plan, build and --apply --------------------------------------------------

LAW = "in_game/common/laws/l.txt"
FE = "in_game/common/laws/fe.txt"
DEFS = "in_game/common/x/defs.txt"


def _mod(tmp_path, monkeypatch):
    """A mod with merged copies, new definitions and a removed block in one file."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    v1 = {LAW: "law_a = {\n\tcost = 1\n\tx = 1\n\ty = 1\n}\nlaw_b = {\n\tz = 1\n}\n",
          DEFS: "B = {\n\tv = 1\n}\n\nD = {\n\tv = 1\n}\n\nE = {\n\tv = 1\n}\n"}
    v2 = {LAW: "law_a = {\n\tcost = 2\n\tx = 3\n\tnew = 1\n}\n",
          DEFS: "A = {\n\tv = 1\n}\n\nB = {\n\tv = 1\n}\n\nC = {\n\tv = 1\n}\n\nD = {\n\tv = 2\n}\n\n"
                "E = {\n\tv = 2\n}\n\nG = {\n\tv = 1\n}\n"}
    tr = build_tracker(tmp_path, [("1.0", v1), ("1.1", v2)])
    mod = tmp_path / "mod"
    _write_tree(mod, {".metadata/metadata.json": '{"id": "t"}',
                      FE: "REPLACE:law_a = {\n\tcost = 5\n\tx = 1\n\ty = 1\n}\n\nREPLACE:law_b = {\n\tz = 2\n}\n",
                      DEFS: "B = {\n\tv = 1\n}\n\nD = {\n\tv = 1\n\tmine = 1\n}\n"})
    return mod, ["--mod-root", str(mod), "--vanilla-repo", tr.repo, "--old", "1.0", "--new", "1.1"]


def _dry_run(common, tmp_path, *extra):
    plan = tmp_path / "plan.json"
    assert merge_cli.main(common + ["--dry-run", "--quiet", "--plan-out", str(plan), *extra]) == 0
    return json.loads(plan.read_text(encoding="utf-8")), plan


def _choices_file(tmp_path, p, sets):
    path = tmp_path / "choices.json"
    files = {f["file"]: dict(sets[f["file"]], sha=f["before_sha"]) for f in p["files"] if f["file"] in sets}
    path.write_text(json.dumps({"files": files}), encoding="utf-8")
    return path


BUILT = ("merged", "decisions", "open", "removed_check", "stale_entries", "format", "diff", "choices", "not_set",
         "own")


def test_the_dry_run_build_and_apply_give_one_file_for_each_set_of_choices(tmp_path, monkeypatch):
    mod, common = _mod(tmp_path, monkeypatch)
    plain, _ = _dry_run(common, tmp_path)
    assert {f["file"] for f in plain["files"]} == {FE, DEFS}
    rng = random.Random(3)
    for n in range(12):
        sets = {}
        for f in plain["files"]:
            keys = [merge_cli.choice_key(d) for d in f["decisions"] if d.get("address")]
            sets[f["file"]] = {"all": rng.choice((None, merge.TAKE, merge.KEEP)),
                               "nodes": {k: rng.choice(merge_cli.CHOICES) for k in keys if rng.random() < 0.5}}
        p, _plan = _dry_run(common, tmp_path, "--choices", str(_choices_file(tmp_path, plain, sets)))
        again = json.loads(json.dumps(plain))
        merge_cli.build(again["files"], {rel: rec for rel, rec in sets.items()})
        for a, b in zip(p["files"], again["files"]):
            assert {k: a.get(k) for k in BUILT} == {k: b.get(k) for k in BUILT}
    # --apply with the choices builds the file again from the plan without choices.
    sets = {FE: {"all": merge.KEEP, "nodes": {}}, DEFS: {"all": merge.TAKE, "nodes": {}}}
    choices = _choices_file(tmp_path, plain, sets)
    p, _plan = _dry_run(common, tmp_path, "--choices", str(choices))
    _p, plan = _dry_run(common, tmp_path)
    assert merge_cli.main(common + ["--apply", str(plan), "--choices", str(choices)]) == 0
    for f in p["files"]:
        assert merge_cli.read_mod_file(mod / f["file"])[0] == f["merged"]


def test_choose_takes_or_keeps_every_decision_of_a_file(tmp_path, monkeypatch, capsys):
    mod, common = _mod(tmp_path, monkeypatch)
    p, _ = _dry_run(common, tmp_path, "--choose", "keep")
    for f in p["files"]:
        assert f["merged"] == f["gathered"]["ours"] and not f["open"]
        assert all(d["action"] == merge.KEEP for d in f["decisions"])
        assert f["choices"] == {"all": "keep", "nodes": {}} and f["not_set"] == []
    p, _ = _dry_run(common, tmp_path, "--choose", "take", "--file", DEFS)
    [f] = p["files"]
    assert not f["open"]
    assert all(d["action"] == merge.TAKE for d in f["decisions"])
    assert [d["path"][0]["key"] for d in f["decisions"]] == ["A", "C", "v", "E", "G"]   # file order; v is in D
    assert "\nC = {\n\tv = 1\n}\n" in f["merged"] and "E = {\n\tv = 2\n}" in f["merged"]
    assert "D = {\n\tv = 2\n\tmine = 1\n}" in f["merged"]


def test_a_choice_for_one_decision_comes_before_the_choice_for_all(tmp_path, monkeypatch):
    mod, common = _mod(tmp_path, monkeypatch)
    plain, _ = _dry_run(common, tmp_path, "--file", DEFS)
    [f] = plain["files"]
    g = next(d for d in f["decisions"] if d["path"][0]["key"] == "G")
    sets = {DEFS: {"all": merge.TAKE, "nodes": {merge_cli.choice_key(g): merge.KEEP}}}
    merge_cli.build(plain["files"], sets)
    got = {d["path"][0]["key"]: d["action"] for d in f["decisions"]}
    assert got == {"v": "take", "A": "take", "C": "take", "E": "take", "G": "keep"}
    assert "G = {" not in f["merged"]


def test_a_decision_that_takes_no_choice_says_why(tmp_path, monkeypatch, capsys):
    """Vanilla removes a whole file that the mod holds. --apply never deletes a file,
    so Take vanilla for all leaves that decision open and names the reason once."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    gone = "in_game/common/x/gone.txt"
    tr = build_tracker(tmp_path, [("1.0", {gone: "G = {\n\tv = 1\n}\n", DEFS: "Z = {\n\tv = 1\n}\n"}),
                                  ("1.1", {DEFS: "Z = {\n\tv = 1\n}\n"})])
    mod = tmp_path / "mod"
    _write_tree(mod, {".metadata/metadata.json": '{"id": "t"}', gone: "G = {\n\tv = 3\n}\n"})
    common = ["--mod-root", str(mod), "--vanilla-repo", tr.repo, "--old", "1.0", "--new", "1.1"]
    plan = tmp_path / "plan.json"
    assert merge_cli.main(common + ["--dry-run", "--choose", "take", "--plan-out", str(plan)]) == 0
    out = capsys.readouterr().out
    [f] = json.loads(plan.read_text(encoding="utf-8"))["files"]
    assert f["open"] == 1
    assert f["not_set"] == [["--apply does not delete a file: delete it by hand, or keep it", 1]]
    assert "Take vanilla for all does not apply to 1 decision: --apply does not delete a file" in out
    [row] = f["gathered"]["removed"]
    assert row["take"][0] == merge.OPEN and row["keep"][0] == merge.KEEP


@pytest.mark.parametrize("all_choice", [merge.TAKE, merge.KEEP])
def test_a_memo_gives_the_same_file_as_a_build_from_nothing(tmp_path, monkeypatch, all_choice):
    mod, common = _mod(tmp_path, monkeypatch)
    plain, _ = _dry_run(common, tmp_path)
    memo, files = {}, json.loads(json.dumps(plain["files"]))
    merge_cli.build(files, {}, memo)
    for f in files:
        keys = [merge_cli.choice_key(d) for d in f["decisions"] if d.get("address")]
        for k in keys:
            sets = {f["file"]: {"all": all_choice, "nodes": {k: merge.TAKE}}}
            merge_cli.build([f], sets, memo)
            fresh = json.loads(json.dumps(f))
            merge_cli.build([fresh], sets)
            assert {x: f.get(x) for x in BUILT} == {x: fresh.get(x) for x in BUILT}


def _own_block(f, d, memo_key=("t", "f")):
    from pdxaudit import merge_rows
    merge_rows.load(memo_key, json.loads(json.dumps(f)))
    return merge_rows.own_block(memo_key, d)


def _own(f, d, body):
    """The own text entry for decision `d` of plan file `f`, from the editor's `body`."""
    from pdxaudit.merge_rows import from_body
    block = _own_block(f, d)
    assert not block["problem"]
    return {"span": block["span"], "text": from_body(body, tuple(block["frame"])), "edges": block["edges"]}


def test_your_own_text_takes_the_place_of_the_block_that_holds_a_change(tmp_path, monkeypatch):
    mod, common = _mod(tmp_path, monkeypatch)
    plain, plan = _dry_run(common, tmp_path, "--file", DEFS)
    [f] = plain["files"]
    v = next(d for d in f["decisions"] if d["path"][0]["key"] == "v")
    block = _own_block(f, v)
    assert block["mine"] == "D = {\n\tv = 1\n\tmine = 1\n}"          # the whole block D, not the line of v
    assert block["result"] == "D = {\n\tv = 2\n\tmine = 1\n}" == block["vanilla"]
    a = next(d for d in f["decisions"] if d["path"][0]["key"] == "A")
    own_a = _own(f, a, "A = {\n\tv = 9\n}\n\n")      # a node your file does not hold: its insertion
    sets = {DEFS: {"all": "take", "nodes": {merge_cli.choice_key(v): "keep"},
                   "own": [_own(f, v, "D = {\n\tv = 7\n\tmine = 2\n}"), own_a]}}
    p, _ = _dry_run(common, tmp_path, "--file", DEFS, "--choices", str(_choices_file(tmp_path, plain, sets)))
    [f] = p["files"]
    assert "D = {\n\tv = 7\n\tmine = 2\n}" in f["merged"] and "A = {\n\tv = 9\n}" in f["merged"]
    got = {d["path"][0]["key"]: (d["action"], bool(d.get("own"))) for d in f["decisions"]}
    assert got["v"] == ("take", True) and got["A"] == ("take", True) and got["C"] == ("take", False)
    assert [o["decisions"] for o in f["own"]] == [1, 1] and not f["open"]
    assert merge_cli.main(common + ["--apply", str(plan), "--file", DEFS, "--choices",
                                    str(_choices_file(tmp_path, plain, sets))]) == 0
    assert (mod / DEFS).read_text(encoding="utf-8") == f["merged"]


def test_the_result_saved_as_your_own_text_gives_the_same_file(tmp_path, monkeypatch):
    """The editor starts from the block as Apply file writes it. Saved as it is, your
    own text changes nothing in the file, for each decision and each choice for all."""
    mod, common = _mod(tmp_path, monkeypatch)
    plain, _ = _dry_run(common, tmp_path)
    checked = 0
    for all_choice in (None, merge.TAKE, merge.KEEP):
        files = json.loads(json.dumps(plain["files"]))
        sets = {f["file"]: {"all": all_choice, "nodes": {}} for f in files}
        merge_cli.build(files, sets)
        for f in files:
            for d in f["decisions"]:
                if d.get("span") is None:
                    continue
                block = _own_block(f, d)
                own = dict(sets[f["file"]], own=[_own(f, d, block["result"])])
                again = json.loads(json.dumps(f))
                merge_cli.build([again], {f["file"]: own})
                assert again["merged"] == f["merged"] and again["open"] <= f["open"]
                checked += 1
    assert checked >= 10


def test_your_own_text_that_cannot_go_in_leaves_its_changes_open(tmp_path, monkeypatch):
    """A text whose blocks do not close, or a place that an edit of the merge crosses,
    does not go in. The changes it holds are open, so --apply does not write the file."""
    mod, common = _mod(tmp_path, monkeypatch)
    plain, plan = _dry_run(common, tmp_path, "--file", DEFS)
    [f] = plain["files"]
    v = next(d for d in f["decisions"] if d["path"][0]["key"] == "v")
    bad = _own(f, v, "D = {\n\tv = 7\n")
    assert merge_cli.own_problem(bad["text"]) == "your text leaves 1 block open"
    files = json.loads(json.dumps(plain["files"]))
    merge_cli.build(files, {DEFS: {"all": None, "nodes": {}, "own": [bad]}})
    [f2] = files
    assert f2["merged"] == f["merged"] and f2["open"] == f["open"] + 1
    v2 = next(d for d in f2["decisions"] if d["path"][0]["key"] == "v")
    assert v2["action"] == "open" and "cannot go in: your text leaves 1 block open" in v2["reason"]
    # A place that cuts an edit of the merge in two.
    s, e = v["span"]
    assert e - s > 1
    cut = {"span": [s + 1, e + 3], "text": "x"}
    files = json.loads(json.dumps(plain["files"]))
    merge_cli.build(files, {DEFS: {"all": None, "nodes": {}, "own": [cut]}})
    assert files[0]["open"] == f["open"] + 1 and "crosses an edge" in next(
        d for d in files[0]["decisions"] if d["path"][0]["key"] == "v")["reason"]
    choices = _choices_file(tmp_path, plain, {DEFS: {"all": None, "nodes": {}, "own": [bad]}})
    assert merge_cli.main(common + ["--apply", str(plan), "--file", DEFS, "--choices", str(choices)]) == 1
    assert (mod / DEFS).read_text(encoding="utf-8") == f["gathered"]["ours"]


def test_own_entries_drop_a_span_outside_the_file_and_an_overlap():
    text = "a = {\n\tb = 1\n}\n"
    rows = [{"span": [0, 5], "text": "x"}, {"span": [3, 8], "text": "y"}, {"span": [8, 99], "text": "z"},
            {"span": [8, 8], "text": "w"}, {"span": [8, 8], "text": "w2"}, "bad"]
    assert merge_cli.own_entries(rows, text) == [{"span": [0, 5], "text": "x", "edges": [True, True]},
                                                 {"span": [8, 8], "text": "w", "edges": [True, True]}]


def test_saved_choices_belong_to_their_versions_and_the_cli_reads_and_clears_them(tmp_path, monkeypatch):
    from pdxaudit.store import open_store
    mod, common = _mod(tmp_path, monkeypatch)
    plain, _ = _dry_run(common, tmp_path, "--file", DEFS)
    [f] = plain["files"]
    g = next(d for d in f["decisions"] if d["path"][0]["key"] == "G")
    store, _err = open_store(mod)
    files = {DEFS: {"sha": f["before_sha"], "all": None, "nodes": {merge_cli.choice_key(g): "keep"}}}
    merge_cli.save_choices(store, "1.0", "1.1", files)
    assert merge_cli.saved_choices(store, "1.0", "1.1")["files"] == files
    assert merge_cli.saved_choices(store, "0.9", "1.1")["files"] == {}       # other versions: none
    p, _ = _dry_run(common, tmp_path, "--file", DEFS, "--saved")
    assert next(d for d in p["files"][0]["decisions"] if d["path"][0]["key"] == "G")["action"] == "keep"
    assert merge_cli.main(common + ["--clear-saved"]) == 0
    assert not (store.dir / merge_cli.SAVED_CHOICES).exists()
