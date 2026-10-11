"""Test setup: put the repo root on sys.path, and provide a `world` fixture
that builds a tiny synthetic vanilla-tracker (a real bare git repo with two
commits) plus a matching mod, so the git-dependent audits can be exercised
end-to-end without the real 450 MB tracker.

Each test that builds a tracker (build_tracker or `world`) runs twice: with a bare
tracker that --commit made, and with a git repository of the whole game install
that a user keeps with plain git. A test that makes its own layout carries the
`own_layout` mark."""
import os
import re
import sys
import types
import subprocess
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pdxaudit.safety import remove_file  # noqa: E402


# Two synthetic vanilla snapshots. Between them vanilla: adds `upkeep` to a
# building block and drops `legacy_mod`; changes a GUI template; and rewor* a
# loc string. The mod overrides all three with the OLD content, so each audit
# has a real finding to report.
VANILLA_OLD = {
    "in_game/common/building_types/b.txt":
        "some_building = {\n\tcost = 100\n\tlegacy_mod = 1\n}\n",
    "in_game/common/buildings/farm.txt":
        "building_farm = {\n\tcost = 50\n}\n",
    # `bar` is defined here so a mod file sorting AFTER vanilla.gui shadows it
    # to no effect (the dead-shadow case); `my_value` is a script value, not a
    # top-level block, so a REPLACE of it exercises the not-found bucketing.
    "in_game/gui/vanilla.gui":
        "template foo = {\n\tsize = { 10 10 }\n}\n"
        "template bar = {\n\tx = 1\n}\n",
    "in_game/common/script_values/v.txt": "my_value = 5\n",
    "in_game/localization/english/v_l_english.yml":
        'l_english:\n KEY_A:0 "old text"\n',
}
VANILLA_NEW = {
    "in_game/common/building_types/b.txt":
        "some_building = {\n\tcost = 100\n\tupkeep = 5\n}\n",
    "in_game/common/buildings/farm.txt":
        "building_granary = {\n\tcost = 50\n}\n",
    "in_game/gui/vanilla.gui":
        "template foo = {\n\tsize = { 20 20 }\n}\n"
        "template bar = {\n\tx = 1\n}\n",
    "in_game/common/script_values/v.txt": "my_value = 5\n",
    "in_game/localization/english/v_l_english.yml":
        'l_english:\n KEY_A:0 "new text"\n',
}
MOD = {
    ".metadata/metadata.json": '{"name":"Test Mod","id":"testmod"}',
    "in_game/common/building_types/m.txt":
        "REPLACE:some_building = {\n\tcost = 100\n\tlegacy_mod = 1\n}\n",
    "in_game/common/rules/r.txt":
        "some_rule = {\n\thas_building = building_farm\n}\n",
    "in_game/gui/aaa_mod.gui":
        "template foo = {\n\tsize = { 10 10 }\n}\n",
    # zzz_mod.gui sorts after vanilla.gui, so its `bar` shadow never applies.
    "in_game/gui/zzz_mod.gui":
        "template bar = {\n\tx = 1\n}\n",
    "in_game/common/script_values/m2.txt": "REPLACE:my_value = 5\n",
    "in_game/localization/english/m_l_english.yml":
        'l_english:\n KEY_A:0 "mod text"\n',
    # the same mod-only name defined in two files: a duplicate definition
    "in_game/common/buildings/dup1.txt": "dup_thing = {\n\tcost = 1\n}\n",
    "in_game/common/buildings/dup2.txt": "dup_thing = {\n\tcost = 2\n}\n",
}


def _write_tree(root, files):
    for rel, content in files.items():
        fp = Path(root) / rel
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")


INDEX_NAME = "fixture.index"


def _commit(repo, files, message, parent, prefix="", extra=None, tag=True):
    """Commit `files` (a {relpath: text} dict) to `repo`, returning the new commit
    hash. Blobs are written straight into git, so no work tree is created; the
    single index file is removed through the removal helper. `prefix` puts the
    files in a folder, `extra` adds files at their own paths, and `tag` tags the
    commit with the first word of `message`."""
    repo = Path(repo)
    idx = repo / INDEX_NAME
    env = {**os.environ, "GIT_DIR": str(repo), "GIT_INDEX_FILE": str(idx),
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}

    def g(*a, stdin=None):
        return subprocess.run(["git", *a], env=env, check=True, input=stdin,
                              capture_output=True, text=True).stdout.strip()

    try:
        paths = {**{prefix + rel: content for rel, content in files.items()}, **(extra or {})}
        for rel, content in paths.items():
            blob = g("hash-object", "-w", "--stdin", stdin=content)
            g("update-index", "--add", "--cacheinfo", f"100644,{blob},{rel}")
        tree = g("write-tree")
        args = ["commit-tree", tree]
        if parent:
            args += ["-p", parent]
        args += ["-m", message]
        commit = g(*args)
        g("update-ref", "refs/heads/master", commit)
        if tag:
            g("tag", message.split()[0], commit)   # version tag, like do_commit
        return commit
    finally:
        if idx.exists():
            remove_file(idx, repo, re.escape(INDEX_NAME))


# The tracker layouts each test that builds a tracker runs with (see
# pytest_generate_tests): a bare tracker that --commit made, and a git repository
# that the user keeps of the whole game install.
LAYOUTS = ("tracker", "install")
_layout = ["tracker"]

# The install's engine folders hold module folders of their own, as EU5's
# `clausewitz` and `jomini` do. They sort before and after `game`.
ENGINE_FILES = {"clausewitz/main_menu/gui/engine.gui": "template engine = {}\n",
                "jomini/in_game/common/x/j.txt": "j = {}\n",
                "binaries/license.txt": "text\n"}
# The game folder has more files than each engine folder, as in a real install,
# also when a snapshot holds one file. The files have no extension, so the tracker
# holds no file format that the snapshots do not give it.
GAME_PADDING = {"main_menu/fixture/pad_a": "pad\n", "main_menu/fixture/pad_b": "pad\n"}


def build_tracker(root, snapshots):
    """The tracker of the test's layout, with one commit per (tag, {relpath: text})
    snapshot, oldest first. Returns SimpleNamespace(repo, hashes={tag: full hash}).
    In the install layout, repo is the `.git` folder of an install folder; its first
    commit holds no game files, and its commits carry no tags."""
    if _layout[0] == "install":
        return build_install(root, snapshots)
    repo = Path(root) / "vanilla-tracker" / "repo.git"
    repo.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "--bare", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "--git-dir", str(repo), "symbolic-ref", "HEAD",
                    "refs/heads/master"], check=True, capture_output=True)
    parent, hashes = None, {}
    for tag, files in snapshots:
        parent = _commit(repo, files, f"{tag} Test", parent)
        hashes[tag] = parent
    return types.SimpleNamespace(repo=str(repo), hashes=hashes)


def build_install(root, snapshots):
    """A git repository of the whole game install, as a user keeps it with plain
    git: the game files under `game/`, engine folders beside it, and untagged
    commits. Returns SimpleNamespace(repo, hashes={tag: full hash})."""
    install = Path(root) / "Europa Universalis V"
    install.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(install)], check=True, capture_output=True)
    repo = install / ".git"
    subprocess.run(["git", "--git-dir", str(repo), "symbolic-ref", "HEAD", "refs/heads/master"],
                   check=True, capture_output=True)
    parent = _commit(repo, {}, "Initial commit", None, extra={"readme.txt": "not game data\n"}, tag=False)
    hashes = {}
    for tag, files in snapshots:
        parent = _commit(repo, {**GAME_PADDING, **files}, f"{tag} Test", parent, prefix="game/",
                         extra=ENGINE_FILES, tag=False)
        hashes[tag] = parent
    return types.SimpleNamespace(repo=str(repo), hashes=hashes)


def _builds_a_tracker(metafunc):
    """True when the test reads a tracker that build_tracker or `world` makes."""
    import inspect
    if metafunc.definition.get_closest_marker("own_layout"):
        return False
    if "world" in metafunc.fixturenames:
        return True
    try:
        return "build_tracker(" in inspect.getsource(metafunc.module)
    except (OSError, TypeError):
        return False


def pytest_configure(config):
    config.addinivalue_line("markers", "own_layout: the test builds the tracker layout that it reads")


def pytest_generate_tests(metafunc):
    """Each test that builds a tracker runs once for each layout, so that each
    feature works with both from the start."""
    if "tracker_layout" in metafunc.fixturenames and _builds_a_tracker(metafunc):
        metafunc.parametrize("tracker_layout", LAYOUTS, indirect=True)


@pytest.fixture(autouse=True)
def tracker_layout(request):
    """The layout build_tracker makes in this test."""
    _layout[0] = getattr(request, "param", "tracker")
    yield _layout[0]
    _layout[0] = "tracker"


def make_ctx(repo, new_tag, fixed=False, bases=None, dismissed=None):
    """The run context the CLI hands to audits."""
    from pdxaudit.tracker import get_commits
    return types.SimpleNamespace(commits=get_commits(repo), new_tag=new_tag,
                                 fixed_window=fixed, bases=bases or {}, scanned={},
                                 dismissed=set(dismissed or ()))


def audit_args(**kw):
    base = dict(diff=False, block=None, category=None, full=False, old=None, new=None)
    base.update(kw)
    return types.SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def _own_data_folder(tmp_path, monkeypatch):
    """Each test has its own per-user data folder, so the tracker caches and the
    records a test writes never reach the user's."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "xdg-data"))


@pytest.fixture(autouse=True)
def _merge_takes(monkeypatch):
    """The merge takes a change with no conflict (`merge_default` accept), as most tests
    describe. The tests of the setting set their own value."""
    monkeypatch.setenv("PDX_MERGE_DEFAULT", "accept")


@pytest.fixture
def statement_findings(monkeypatch):
    """Each change in a REPLACE is a finding of its own (`replace_findings` statement),
    for the tests that read one change's finding."""
    monkeypatch.setenv("PDX_REPLACE_FINDINGS", "statement")


@pytest.fixture
def block_findings(monkeypatch):
    """The changes of one REPLACE are one finding, the default."""
    monkeypatch.setenv("PDX_REPLACE_FINDINGS", "block")


@pytest.fixture(autouse=True)
def _no_engine_data(monkeypatch):
    """Tests never read the engine data that the user's config names."""
    import pdxaudit.gui_names
    monkeypatch.setattr(pdxaudit.gui_names, "setting", lambda key, flag=None: (None, "a test"))


@pytest.fixture(autouse=True)
def _no_windows_left():
    """After each test, delete each window and other top-level widget it made. A
    closed window is not deleted, and each widget that remains makes the next
    window slower to open, so without this each test runs slower than the one
    before it."""
    yield
    if "PySide6.QtWidgets" not in sys.modules:
        return
    import shiboken6
    from PySide6 import QtWidgets
    app = QtWidgets.QApplication.instance()
    if app is None:
        return
    for w in app.topLevelWidgets():
        if shiboken6.isValid(w) and w.parent() is None:
            shiboken6.delete(w)
    app.processEvents()


@pytest.fixture
def world(tmp_path):
    tr = build_tracker(tmp_path, [("1.0.0", VANILLA_OLD), ("1.1.0", VANILLA_NEW)])
    repo, old, new = tr.repo, tr.hashes["1.0.0"], tr.hashes["1.1.0"]

    mod = tmp_path / "mod"
    _write_tree(mod, MOD)

    args = types.SimpleNamespace(
        diff=False, block=None, category=None,
        full=True, old=None, new=None)   # full=True: fixed window
    return types.SimpleNamespace(repo=str(repo), old=old, new=new, mod=mod, args=args)
