"""Flattening vanilla and foundations into one base: each directive against a present
and a missing unit, later layers replacing exact paths, names and loc keys, file
name order within one source, merging types, a foundation built on another, merged
units compared by diff3, the stack's points, and a stack run end to end."""
import io
import json
from contextlib import redirect_stdout

import pytest

from conftest import _write_tree, audit_args, build_tracker, make_ctx
from pdxaudit import diff3, flatten
from pdxaudit import sources as S
from pdxaudit.base import StackBase, VanillaBase, stack_points
from pdxaudit.gui import run_gui_audit
from pdxaudit.ledger import finding_id
from test_sources import git_source, make_mod

CAT = "in_game/common/things"


class Fake:
    """A source whose versions are {commit: {path: text}}."""

    def __init__(self, sid, versions):
        self.id, self.key, self.git_dir, self._v = sid, f"{sid}-000000", f"fake:{sid}", versions

    def tree_files(self, commit):
        return [(p, f"{commit}|{p}") for p in self._v[commit]]

    def read_blobs(self, ids):
        return {i: self._v[i.split("|", 1)[0]][i.split("|", 1)[1]].encode() for i in ids}


def layer(sid, files, version="1"):
    return (Fake(sid, {"c": files}), version, "c")


VANILLA = {(CAT, "a"): (f"{CAT}/v.txt", "a = {\n\tx = 1\n}"),
           (CAT, "b"): (f"{CAT}/v.txt", "b = {\n\ty = 1\n}")}


# --- directives ------------------------------------------------------------------------------

@pytest.mark.parametrize("directive,present,missing", [
    ("REPLACE", "a = {\n\tx = 2\n}", None),
    ("TRY_REPLACE", "a = {\n\tx = 2\n}", None),
    ("REPLACE_OR_CREATE", "a = {\n\tx = 2\n}", "gone = {\n\tx = 2\n}"),
    ("INJECT", "a = {\n\tx = 1\n\tx = 2\n}", None),
    ("TRY_INJECT", "a = {\n\tx = 1\n\tx = 2\n}", None),
    ("INJECT_OR_CREATE", "a = {\n\tx = 1\n\tx = 2\n}", "gone = {\n\tx = 2\n}"),
])
def test_each_directive_against_a_present_and_a_missing_unit(directive, present, missing):
    files = {f"{CAT}/f.txt": f"{directive}:a = {{\n\tx = 2\n}}\n{directive}:gone = {{\n\tx = 2\n}}\n"}
    idx, owners = flatten.flatten_blocks(VANILLA, "1.0", [layer("f", files)], [CAT], set())
    assert idx[(CAT, "a")][1] == present
    assert (idx.get((CAT, "gone")) or (None, None))[1] == missing
    assert idx[(CAT, "b")] == VANILLA[(CAT, "b")] and (CAT, "b") not in owners
    if directive.startswith("INJECT") or directive == "TRY_INJECT":
        assert [p["layer"] for p in owners[(CAT, "a")]["parts"]] == ["vanilla", "f"]
    else:
        assert owners[(CAT, "a")] == {"layer": "f", "version": "1", "file": f"{CAT}/f.txt"}


def test_a_later_layer_replaces_an_exact_path_and_a_plain_name():
    files = {f"{CAT}/v.txt": "a = {\n\tx = 9\n}\n", f"{CAT}/other.txt": "c = {\n}\n"}
    idx, owners = flatten.flatten_blocks(VANILLA, "1.0", [layer("f", files)], [CAT], set())
    assert (CAT, "b") not in idx                                  # the replaced file no longer has b
    assert idx[(CAT, "a")] == (f"{CAT}/v.txt", "a = {\n\tx = 9\n}") and owners[(CAT, "a")]["layer"] == "f"
    second = layer("g", {f"{CAT}/g.txt": "a = {\n\tx = 10\n}\n"})
    idx, owners = flatten.flatten_blocks(VANILLA, "1.0", [layer("f", files), second], [CAT], set())
    assert idx[(CAT, "a")][1] == "a = {\n\tx = 10\n}" and owners[(CAT, "a")]["layer"] == "g"


def test_the_alphanumerically_first_file_name_wins_within_one_source():
    files = {f"{CAT}/zzz_last.txt": "a = {\n\tx = 3\n}\n", f"{CAT}/B_second.txt": "a = {\n\tx = 2\n}\n",
             f"{CAT}/00_first.txt": "a = {\n\tx = 1\n}\n"}
    idx, _owners = flatten.flatten_blocks({}, "1.0", [layer("f", files)], [CAT], set())
    assert idx[(CAT, "a")] == (f"{CAT}/00_first.txt", "a = {\n\tx = 1\n}")
    files.pop(f"{CAT}/00_first.txt")
    idx, _owners = flatten.flatten_blocks({}, "1.0", [layer("f", files)], [CAT], set())
    assert idx[(CAT, "a")][0] == f"{CAT}/B_second.txt"


def test_merging_types_combine_in_load_order_and_effect_is_replaced():
    cat = "in_game/common/on_action"
    vanilla = {(cat, "on_start"): (f"{cat}/v.txt", "on_start = {\n\tevents = { a.1 }\n\teffect = { x = 1 }\n}")}
    one = layer("one", {f"{cat}/b.txt": "on_start = {\n\tevents = { f.2 }\n}\n",
                        f"{cat}/a.txt": "on_start = {\n\ton_actions = { f1 }\n\teffect = { y = 1 }\n}\n"})
    two = layer("two", {f"{cat}/x.txt": "on_start = {\n\ttrigger = { t = 1 }\n}\n"})
    idx, owners = flatten.flatten_blocks(vanilla, "1.0", [one, two], [cat], {"common/on_action"})
    text = idx[(cat, "on_start")][1]
    order = [text.index(s) for s in ("a.1", "f1", "y = 1", "f.2", "t = 1")]
    assert order == sorted(order) and "x = 1" not in text
    assert [p["layer"] for p in owners[(cat, "on_start")]["parts"]] == ["vanilla", "one", "one", "two"]


def test_a_foundation_built_on_another_is_flattened_over_both():
    base_layer = layer("frame", {f"{CAT}/frame.txt": "u = {\n\tp = 1\n}\n"})
    top = layer("addon", {f"{CAT}/addon.txt": "INJECT:u = {\n\tq = 1\n}\nREPLACE:a = {\n\tx = 5\n}\n"})
    idx, owners = flatten.flatten_blocks(VANILLA, "1.0", [base_layer, top], [CAT], set())
    assert idx[(CAT, "u")][1] == "u = {\n\tp = 1\n\tq = 1\n}"
    assert owners[(CAT, "u")]["layer"] == "frame" and owners[(CAT, "u")]["parts"][-1]["layer"] == "addon"
    assert owners[(CAT, "a")]["layer"] == "addon"


def test_a_sources_texts_read_with_unix_line_endings():
    crlf = Fake("f", {"c": {f"{CAT}/f.txt": "a = {\r\n\tx = 1\r\n}\r\n"}})
    assert flatten.layer_texts(crlf, "c", flatten._in_categories([CAT]))[0][1] == "a = {\n\tx = 1\n}\n"


def test_a_foundations_missing_inject_or_replace_is_skipped_silently():
    files = {f"{CAT}/f.txt": "INJECT:nothing = {\n\tx = 1\n}\nREPLACE:nothing2 = {\n}\n"}
    idx, owners = flatten.flatten_blocks(VANILLA, "1.0", [layer("f", files)], [CAT], set())
    assert idx == VANILLA and owners == {}


# --- GUI and localization -----------------------------------------------------------------------

GUI_V = ({("in_game", "template", "t"): ("in_game/gui/v.gui", "template t = {\n\tsize = 1\n}"),
          ("in_game", "template", "u"): ("in_game/gui/v.gui", "template u = {\n}")},
         {"in_game/gui/v.gui": "template t = {\n\tsize = 1\n}\ntemplate u = {\n}\n",
          "in_game/gui/w.gui": "template u = {\n\tfrom_w = 1\n}\n"}, [])


def test_gui_names_and_exact_paths_and_file_order():
    f = layer("f", {"in_game/gui/b_f.gui": "template t = {\n\tsize = 3\n}\n",
                    "in_game/gui/A_f.gui": "template t = {\n\tsize = 2\n}\n"})
    defs, files, _bad, owners, file_owners = flatten.flatten_gui(GUI_V, "1.0", [f], ["in_game"])
    assert defs[("in_game", "template", "t")] == ("in_game/gui/A_f.gui", "template t = {\n\tsize = 2\n}")
    assert owners[("in_game", "template", "t")]["layer"] == "f" and file_owners["in_game/gui/b_f.gui"]["layer"] == "f"

    g = layer("g", {"in_game/gui/v.gui": "template t = {\n\tsize = 9\n}\n"})
    defs, files, _bad, owners, _fo = flatten.flatten_gui(GUI_V, "1.0", [g], ["in_game"])
    assert files["in_game/gui/v.gui"] == "template t = {\n\tsize = 9\n}\n"
    assert defs[("in_game", "template", "u")][0] == "in_game/gui/w.gui"      # the shadowed copy comes back


def test_loc_keys_replace_folder_first_and_later_layers_win():
    wanted = {("english", "K"), ("english", "L")}
    f = layer("f", {"main_menu/localization/english/a_l_english.yml": 'l_english:\n K:0 "normal"\n L:0 "f"\n',
                    "main_menu/localization/english/replace/z_l_english.yml": 'l_english:\n K:0 "replaced"\n'})
    g = layer("g", {"main_menu/localization/english/g_l_english.yml": 'l_english:\n L:0 "g"\n'})
    values, owners = flatten.flatten_loc({("english", "K"): "vanilla"}, [f, g], wanted)
    assert values == {("english", "K"): "replaced", ("english", "L"): "g"}
    assert owners[("english", "K")]["layer"] == "f" and owners[("english", "L")]["layer"] == "g"


# --- merged units in diff3 ----------------------------------------------------------------------

def test_merged_plain_statements_compare_as_a_multiset():
    merged = flatten.merge_text("o = {\n\tevents = { a }\n}", "o = {\n\tevents = { b }\n}")
    reordered = "o = {\n\tevents = { b }\n\tevents = { a }\n}"
    assert diff3.distance(diff3.nodes(reordered), diff3.nodes(merged)) == 0


def test_repeated_same_key_blocks_pair_in_load_order():
    merged = flatten.merge_text("o = {\n\tif = { a = 1 }\n}", "o = {\n\tif = { b = 1 }\n}")
    copy = "o = {\n\tif = { a = 1 }\n\tif = { b = 2 }\n}"
    changes = diff3.compare(copy, [merged])
    assert [(c.kind, " ".join(merged[c.new.start:c.new.end].split())) for c in changes] == \
        [("mod_changed", "b = 1")]


# --- the stack's points and a stack run ---------------------------------------------------------------

@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("PDX_GAME_ROOT", str(tmp_path / "no-game"))


def _patch(src, patches):
    info = S.key_info(src.key)
    info["patches"] = {t: {"patch": p, "how": "chosen"} for t, p in patches.items()}
    S.save_key_info(src.key, info)


def test_points_place_patched_versions_and_leave_unpatched_ones_out(tmp_path, data):
    repo = build_tracker(tmp_path, [("1.0", {"x.txt": "1"}), ("1.1", {"x.txt": "2"}), ("1.2", {"x.txt": "3"})]).repo
    mod = make_mod(tmp_path / "mod")
    S.add_source(mod, git_source(tmp_path / "a", [("a1", {}), ("a2", {}), ("a3", {})], mod_id="a"), "foundation",
                 vanilla_repo=repo)
    S.add_source(mod, git_source(tmp_path / "b", [("b1", {}), ("b2", {})], mod_id="b"), "foundation",
                 vanilla_repo=repo)
    a, b = S.load_sources(mod).foundations
    _patch(a, {"a1": "1.0", "a2": "1.1", "a3": None})
    _patch(b, {"b1": "1.1", "b2": "1.1"})
    points = stack_points(repo, [a, b])
    assert [p.msg.tag for p in points] == ["1.0", "a a1", "1.1", "a a2", "b b1", "b b2", "1.2"]
    assert [p.layer for p in points] == ["vanilla", "a", "vanilla", "a", "b", "b", "vanilla"]
    assert points[-1].members == (points[3].members[0], points[5].members[1])
    assert len({p.id for p in points}) == len(points)
    assert [p.msg.tag for p in stack_points(repo, [a, b], {"b": "b1"})][-2:] == ["b b1", "1.2"]


GUI_OLD = {"in_game/gui/v.gui": "template t1 = {\n\tsize = 1\n}\n"}
GUI_NEW = {"in_game/gui/v.gui": "template t1 = {\n\tsize = 1\n}\n"}


def _stack_world(tmp_path):
    repo = build_tracker(tmp_path, [("1.0", GUI_OLD), ("1.1", GUI_NEW)]).repo
    found = git_source(tmp_path / "found", [
        ("2.0", {"in_game/gui/found.gui": "template t1 = {\n\tsize = 1\n\textra = 2\n}\n"
                                          "template c1 = {\n\ta = 1\n}\n"}),
        ("2.1", {"in_game/gui/found.gui": "template t1 = {\n\tsize = 1\n\textra = 2\n}\n"
                                          "template c1 = {\n\ta = 2\n}\n"})], mod_id="found")
    mod = make_mod(tmp_path / "mod")
    _write_tree(mod, {"in_game/gui/mine.gui": "template t1 = {\n\tsize = 1\n\textra = 2\n}\n"
                                              "template c1 = {\n\ta = 1\n}\n"})
    S.add_source(mod, found, "foundation", vanilla_repo=repo)
    src = S.load_sources(mod).foundations[0]
    _patch(src, {"2.0": "1.0", "2.1": "1.1"})
    return repo, mod, src


def test_a_stack_run_owns_findings_by_foundation_and_reports_duplicates(tmp_path, data):
    repo, mod, src = _stack_world(tmp_path)
    base = StackBase(repo, [src])
    commits = base.commits()
    ctx = make_ctx(repo, "found 2.1")
    ctx.commits = commits
    with redirect_stdout(io.StringIO()) as buf:
        findings = run_gui_audit(mod, base, commits[1][0], commits[1][1], commits[0][0], commits[0][1],
                                 audit_args(), ctx)
    out = buf.getvalue()
    by = {f.name: f for f in findings}
    assert by["t1"].kind == "foundation_duplicate" and by["t1"].key == {"target": "gui:in_game/template/t1",
                                                                         "base": "found"}
    c1 = by["c1"]
    assert c1.kind == "gui_vanilla_changed_mid" and c1.key["base"] == "found" and c1.since == "found 2.1"
    assert "changes foundation content" in out and "Identical to a Foundation's (1)" in out
    assert list((S.data_root() / "stacks" / base.stack_hash / "cache").glob("gui-v1-*.json"))


def test_the_stack_cache_is_pruned_when_a_patch_changes(tmp_path, data):
    repo, mod, src = _stack_world(tmp_path)
    base = StackBase(repo, [src])
    for pid, _m in base.commits():
        base.gui_index(pid, ["in_game"])
    cache = S.data_root() / "stacks" / base.stack_hash / "cache"
    before = {p.name for p in cache.glob("*.json")}
    _patch(src, {"2.0": "1.1", "2.1": "1.1"})
    changed = StackBase(repo, [src])
    changed.prune()
    after = {p.name for p in cache.glob("*.json")}
    assert after < before and all(any(pid in n for pid in changed.by_id) for n in after)


def test_a_vanilla_base_gives_the_same_output_and_ids_as_the_tracker_path(world):
    ctx = make_ctx(world.repo, "1.1.0")
    runs = []
    for base in (world.repo, VanillaBase(world.repo)):
        with redirect_stdout(io.StringIO()) as buf:
            findings = run_gui_audit(world.mod, base, world.old, "1.0.0 Test", world.new, "1.1.0 Test",
                                     world.args, ctx)
        runs.append((buf.getvalue(), sorted(finding_id(f) for f in findings)))
    assert runs[0] == runs[1] and runs[0][1]
