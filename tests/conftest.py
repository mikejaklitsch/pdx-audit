"""Test setup: put the repo root on sys.path, and provide a `world` fixture
that builds a tiny synthetic vanilla-tracker (a real bare git repo with two
commits) plus a matching mod, so the git-dependent audits can be exercised
end-to-end without the real 450 MB tracker."""
import os
import sys
import types
import shutil
import tempfile
import subprocess
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# Two synthetic vanilla snapshots. Between them vanilla: adds `upkeep` to a
# building block and drops `legacy_mod`; changes a GUI template; and rewor* a
# loc string. The mod overrides all three with the OLD content, so each audit
# has a real finding to report.
VANILLA_OLD = {
    "in_game/common/building_types/b.txt":
        "some_building = {\n\tcost = 100\n\tlegacy_mod = 1\n}\n",
    "in_game/common/buildings/farm.txt":
        "building_farm = {\n\tcost = 50\n}\n",
    "in_game/gui/vanilla.gui":
        "template foo = {\n\tsize = { 10 10 }\n}\n",
    "in_game/localization/english/v_l_english.yml":
        'l_english:\n KEY_A:0 "old text"\n',
}
VANILLA_NEW = {
    "in_game/common/building_types/b.txt":
        "some_building = {\n\tcost = 100\n\tupkeep = 5\n}\n",
    "in_game/common/buildings/farm.txt":
        "building_granary = {\n\tcost = 50\n}\n",
    "in_game/gui/vanilla.gui":
        "template foo = {\n\tsize = { 20 20 }\n}\n",
    "in_game/localization/english/v_l_english.yml":
        'l_english:\n KEY_A:0 "new text"\n',
}
MOD = {
    ".metadata/metadata.json": '{"name":"testmod"}',
    "in_game/common/building_types/m.txt":
        "REPLACE:some_building = {\n\tcost = 100\n\tlegacy_mod = 1\n}\n",
    "in_game/common/rules/r.txt":
        "some_rule = {\n\thas_building = building_farm\n}\n",
    "in_game/gui/aaa_mod.gui":
        "template foo = {\n\tsize = { 10 10 }\n}\n",
    "in_game/localization/english/m_l_english.yml":
        'l_english:\n KEY_A:0 "mod text"\n',
}


def _write_tree(root, files):
    for rel, content in files.items():
        fp = Path(root) / rel
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")


def _commit(repo, files, message, parent):
    """Commit `files` (a {relpath: text} dict) to bare `repo`, returning the
    new commit hash. Uses a throwaway work tree and index so nothing persists."""
    wt = tempfile.mkdtemp()
    idx = wt + ".index"
    try:
        _write_tree(wt, files)
        env = {**os.environ, "GIT_DIR": str(repo), "GIT_WORK_TREE": wt,
               "GIT_INDEX_FILE": idx,
               "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}

        def g(*a):
            return subprocess.run(["git", *a], env=env, check=True,
                                  capture_output=True, text=True).stdout.strip()

        g("add", "-A")
        tree = g("write-tree")
        args = ["commit-tree", tree]
        if parent:
            args += ["-p", parent]
        args += ["-m", message]
        commit = g(*args)
        g("update-ref", "refs/heads/master", commit)
        g("tag", message.split()[0], commit)   # version tag, like do_snapshot
        return commit
    finally:
        shutil.rmtree(wt, ignore_errors=True)
        if os.path.exists(idx):
            os.remove(idx)


@pytest.fixture
def world(tmp_path):
    repo = tmp_path / "vanilla-tracker" / "repo.git"
    repo.parent.mkdir(parents=True)
    subprocess.run(["git", "init", "--bare", str(repo)],
                   check=True, capture_output=True)
    subprocess.run(["git", "--git-dir", str(repo), "symbolic-ref",
                    "HEAD", "refs/heads/master"], check=True, capture_output=True)
    old = _commit(repo, VANILLA_OLD, "1.0.0 Test", None)
    new = _commit(repo, VANILLA_NEW, "1.1.0 Test", old)

    mod = tmp_path / "mod"
    _write_tree(mod, MOD)

    args = types.SimpleNamespace(
        diff=False, block=None, category=None,
        full=True, old=None, new=None)   # full=True: fixed window
    return types.SimpleNamespace(repo=str(repo), old=old, new=new, mod=mod, args=args)
