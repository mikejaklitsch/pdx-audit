"""Fork detection by adoption: the fork point is the newest vanilla patch whose
changes the copy mostly contains, so indentation, the copy's own additions and a
few deliberate omissions no longer drag the baseline back to the oldest snapshot."""
from pdxaudit.adoption import detect_fork, first_change_after

V0 = "template t = {\n\ta = 1\n\tb = 2\n}\n"
V1 = "template t = {\n\ta = 1\n\tb = 2\n\tc = 3\n\td = 4\n\te = 5\n\tf = 6\n}\n"
V2 = "template t = {\n\ta = 1\n\tb = 2\n\tc = 3\n\td = 4\n\te = 5\n\tf = 6\n\tg = 7\n\th = 8\n\ti = 9\n}\n"


def test_reindented_copy_with_own_lines_detects_newest_adopted_patch():
    mod = ("template t = {\n    a = 1\n    b = 2\n    c = 3\n    d = 4\n    e = 5\n"
           "    f = 6\n    mine = yes\n    also_mine = 1\n}\n")
    fork, partials = detect_fork(mod, [V0, V1, V2])
    assert fork == 1 and partials == []


def test_deliberate_omission_still_counts_as_adopted():
    mod = "template t = {\n\ta = 1\n\tb = 2\n\tc = 3\n\td = 4\n\te = 5\n}\n"   # f dropped
    fork, partials = detect_fork(mod, [V0, V1])
    assert fork == 1 and partials == []           # 3/4 = 0.75 is not partial


def test_nothing_adopted_gives_no_fork():
    fork, partials = detect_fork(V0, [V0, V1, V2])
    assert fork is None and partials == []


def test_small_patch_cannot_set_fork_on_its_own():
    big_unadopted = "template t = {\n\tx = 1\n\ty = 2\n\tz = 3\n\tw = 4\n}\n"
    small = "template t = {\n\tx = 1\n\ty = 2\n\tz = 3\n\tw = 4\n\tq = 5\n}\n"
    mod = "template t = {\n\tq = 5\n\tunrelated = 1\n}\n"        # has only q
    fork, _ = detect_fork(mod, [V0, big_unadopted, small])
    assert fork is None


def test_small_patch_after_adopted_patch_moves_fork():
    small = V1.replace("\tf = 6\n", "\tf = 6\n\tq = 5\n")
    mod = V1.replace("\tf = 6\n", "\tf = 6\n\tq = 5\n")
    fork, _ = detect_fork(mod, [V0, V1, small])
    assert fork == 2


def test_partly_adopted_patch_is_reported():
    v1 = "template t = {\n" + "".join(f"\tk{i} = {i}\n" for i in range(10)) + "}\n"
    mod = "template t = {\n" + "".join(f"\tk{i} = {i}\n" for i in range(6)) + "}\n"
    fork, partials = detect_fork(mod, ["template t = {\n}\n", v1])
    assert fork == 1
    assert partials == [(1, 6, 10)]


def test_tiny_partly_adopted_patch_is_not_reported():
    v0 = "template t = {\n\tk = 1\n}\n"
    v1 = "template t = {\n\tk = 2\n}\n"        # one line changed: 1 added, 1 removed
    mod = "template t = {\n\tk = 3\n}\n"       # customized: scores 1/2
    fork, partials = detect_fork(mod, [v0, v1])
    assert partials == []


def test_custom_stats_decide_adoption():
    texts = ["a = 1", "a = 2", "a = 3"]
    stats = lambda prev, cur: (3, 3) if cur == "a = 2" else (0, 3)
    fork, _ = detect_fork("anything", texts, stats=stats)
    assert fork == 1


def test_copy_that_appeared_later_starts_fresh():
    fork, _ = detect_fork(V1, [None, V0, V1])
    assert fork == 2


def test_first_change_after():
    texts = ["a = 1", "a = 1", "a = 2", "a =   2", "a = 3"]
    assert first_change_after(texts, 1, 4) == 2
    assert first_change_after(texts, 2, 4) == 4       # whitespace-only is not a change
    assert first_change_after(texts, 4, 4) is None
