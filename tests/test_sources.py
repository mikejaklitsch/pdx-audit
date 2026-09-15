"""Sources: canonical paths and kinds, the registry and a mod's stored choices,
versions and patches, folder snapshots in the shared tracker and its lock,
suggestions and their scan cache, freshness, orphaned sources and per-source cache
pruning."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import _write_tree, build_tracker
from pdx_utilities import paths
from pdxaudit import sources as S
from pdxaudit import tracker as T
from pdxaudit.sources import FOLDER, FOUNDATION, ADOPTED, GIT, SourceError

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@e"}


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("PDX_GAME_ROOT", str(tmp_path / "no-game"))
    return tmp_path / "xdg" / "pdx-audit"


@pytest.fixture
def vanilla(tmp_path):
    return build_tracker(tmp_path, [("1.0.0", {"a.txt": "x"}), ("1.1.0", {"a.txt": "y"})]).repo


def meta(mod_id=None, version=None, deps=()):
    d = {"name": "x", "relationships": [{"rel_type": "dependency", "id": x} for x in deps]}
    if mod_id:
        d["id"] = mod_id
    if version:
        d["version"] = version
    return json.dumps(d)


def make_mod(root, mod_id="my_mod", deps=()):
    _write_tree(root, {".metadata/metadata.json": meta(mod_id, "1.0", deps),
                       "in_game/common/x/a.txt": "a = { }\n"})
    return root


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                          env={**os.environ, **GIT_ENV}).stdout.strip()


def git_source(root, commits, mod_id="found"):
    """A working-copy repository with one commit per (version, files); version None
    leaves the metadata as it was."""
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q", "-b", "main")
    for version, files in commits:
        tree = dict(files)
        if version:
            tree[".metadata/metadata.json"] = meta(mod_id, version)
        _write_tree(root, tree)
        git(root, "add", "-A")
        git(root, "commit", "-q", "--allow-empty", "-m", version or "work")
    return root


# --- canonical paths and kinds --------------------------------------------------------

def test_native_form_for_windows_and_wsl_spellings():
    assert paths.native_form("C:\\Users\\x", wsl=True) == "/mnt/c/Users/x"
    assert paths.native_form("C:/Users/x", wsl=True) == "/mnt/c/Users/x"
    assert paths.native_form("/mnt/c/Users/x", wsl=False, windows=True) == "C:\\Users\\x"
    assert paths.native_form("/home/x", wsl=True) == "/home/x"


def test_path_key_casefolds_windows_mounts_only(monkeypatch):
    monkeypatch.setattr(paths, "_on_wsl", lambda: True)
    assert paths.path_key("/mnt/c/Users/X/") == paths.path_key("C:\\users\\x")
    assert paths.path_key("/home/A") != paths.path_key("/home/a")


def test_kind_detection_and_the_stored_override(tmp_path, data, vanilla):
    workshop = tmp_path / "Steam/steamapps/workshop/content/123/456"
    git_source(workshop, [("1.0", {"in_game/common/x/a.txt": "a = 1\n"})])
    repo = git_source(tmp_path / "dev", [("1.0", {"in_game/common/x/a.txt": "a = 1\n"})])
    plain = tmp_path / "plain"
    _write_tree(plain, {".metadata/metadata.json": meta("plain", "1.0")})
    assert S.detect_kind(workshop) == FOLDER
    assert S.detect_kind(repo) == GIT
    assert S.detect_kind(plain) == FOLDER
    assert S.detect_kind(repo / "in_game") == FOLDER        # inside a repository, not its top

    mod = make_mod(tmp_path / "mod")
    S.add_source(mod, repo, FOUNDATION, kind=FOLDER, vanilla_repo=vanilla)
    assert S.load_sources(mod).by_id("found").kind == FOLDER
    S.set_kind(mod, "found", "auto")
    assert S.load_sources(mod).by_id("found").kind == GIT


# --- the registry and stored choices ------------------------------------------------------

def test_registry_reuses_a_key_by_canonical_path(tmp_path, data, vanilla):
    found = git_source(tmp_path / "found", [("1.0", {})])
    a, b = make_mod(tmp_path / "a", "mod_a"), make_mod(tmp_path / "b", "mod_b")
    S.add_source(a, found, FOUNDATION, vanilla_repo=vanilla)
    S.add_source(b, str(found) + "/", FOUNDATION, vanilla_repo=vanilla)
    assert len(S.registry()) == 1
    key = next(iter(S.registry()))
    assert S.load_sources(a).by_id("found").key == S.load_sources(b).by_id("found").key == key


def test_choices_persist_in_the_data_folder_not_the_mod(tmp_path, data, vanilla):
    found = git_source(tmp_path / "found", [("1.0", {})])
    mod = make_mod(tmp_path / "mod")
    before = sorted(p.relative_to(mod) for p in mod.rglob("*"))
    S.add_source(mod, found, FOUNDATION, vanilla_repo=vanilla)
    stored = json.loads((data / "my_mod" / "sources.json").read_text())
    assert stored["foundations"] == [{"key": S.load_sources(mod).by_id("found").key,
                                      "path": paths.canonical_path(found)}]
    assert sorted(p.relative_to(mod) for p in mod.rglob("*")) == before
    git(tmp_path, "init", "-q", str(mod))
    assert S.load_sources(mod).by_id("found") is not None          # per mod, not per commit


def test_an_id_already_chosen_is_refused_unless_replaced(tmp_path, data, vanilla):
    one = git_source(tmp_path / "one", [("1.0", {})])
    two = git_source(tmp_path / "two", [("1.0", {})])
    mod = make_mod(tmp_path / "mod")
    S.add_source(mod, one, FOUNDATION, vanilla_repo=vanilla)
    with pytest.raises(SourceError, match="--replace"):
        S.add_source(mod, two, ADOPTED, vanilla_repo=vanilla)
    S.add_source(mod, two, ADOPTED, replace=True, vanilla_repo=vanilla)
    sources = S.load_sources(mod)
    assert not sources.foundations and sources.adopted[0].path == paths.canonical_path(two)


def test_relocation_keeps_the_key_and_patches(tmp_path, data, vanilla):
    found = git_source(tmp_path / "found", [("1.0", {})])
    mod = make_mod(tmp_path / "mod")
    S.add_source(mod, found, FOUNDATION, vanilla_repo=vanilla)
    key = S.load_sources(mod).by_id("found").key
    moved = tmp_path / "moved"
    found.rename(moved)

    sources = S.load_sources(mod)
    assert not sources.foundations and [s.id for s in sources.missing] == ["found"]
    assert "--relocate-source found" in sources.warnings()[0]
    with pytest.raises(SourceError, match="--relocate-source found"):
        S.add_source(mod, moved, FOUNDATION, vanilla_repo=vanilla)
    assert S.registry()[key]["path"] != paths.canonical_path(moved)      # nothing relocated on its own

    S.relocate_source(mod, "found", moved)
    src = S.load_sources(mod).by_id("found")
    assert src.key == key and src.path == paths.canonical_path(moved)
    assert src.versions()[-1][2] == "1.1.0"


def test_foundation_order_appends_moves_and_is_stored(tmp_path, data, vanilla):
    mod = make_mod(tmp_path / "mod")
    for name in ("a", "b", "c"):
        S.add_source(mod, git_source(tmp_path / name, [("1.0", {})], mod_id=name), FOUNDATION, vanilla_repo=vanilla)
    assert [s.id for s in S.load_sources(mod).foundations] == ["a", "b", "c"]
    S.move_source(mod, "c", 1)
    assert [s.id for s in S.load_sources(mod).foundations] == ["c", "a", "b"]
    S.set_foundation_order(mod, ["b", "c", "a"])
    assert [s.id for s in S.load_sources(mod).foundations] == ["b", "c", "a"]
    with pytest.raises(SourceError):
        S.move_source(mod, "a", 9)


def test_rename_rules_belong_to_adopted_sources(tmp_path, data, vanilla):
    mod = make_mod(tmp_path / "mod")
    S.add_source(mod, git_source(tmp_path / "f", [("1.0", {})], mod_id="f"), FOUNDATION, vanilla_repo=vanilla)
    S.add_source(mod, git_source(tmp_path / "g", [("1.0", {})], mod_id="g"), ADOPTED, vanilla_repo=vanilla)
    with pytest.raises(SourceError):
        S.add_rename(mod, "f", "old_", "new_")
    S.add_rename(mod, "g", "old_", "new_")
    assert S.load_sources(mod).by_id("g").rename == [{"from": "old_", "to": "new_"}]
    S.remove_rename(mod, "g", "old_")
    assert S.load_sources(mod).by_id("g").rename == []


# --- versions and patches ---------------------------------------------------------------

def test_git_versions_are_metadata_bumps_plus_head(tmp_path, data):
    repo = git_source(tmp_path / "found", [
        ("1.0", {"in_game/a.txt": "1"}), (None, {"in_game/a.txt": "2"}),
        ("1.1", {"in_game/a.txt": "3"}), (None, {"in_game/a.txt": "4"})])
    tags = [t for _c, t in S.metadata_versions(S.git_dir_of(repo), "HEAD")]
    assert tags[:2] == ["1.0", "1.1"] and len(tags) == 3 and len(tags[2]) == 7


def test_patches_default_to_the_newest_tracked_and_history_has_none(tmp_path, data, vanilla):
    repo = git_source(tmp_path / "found", [("1.0", {}), ("1.1", {})])
    mod = make_mod(tmp_path / "mod")
    S.add_source(mod, repo, FOUNDATION, vanilla_repo=vanilla)
    src = S.load_sources(mod).by_id("found")
    assert [(t, p) for _c, t, p in src.versions()] == [("1.0", None), ("1.1", "1.1.0")]

    git_source(repo, [("1.2", {})])
    assert S.record_versions(src, vanilla) == []
    assert src.versions()[-1][1:] == ("1.2", "1.1.0")


def test_run_assignment_and_the_patch_order_rule(tmp_path, data, vanilla):
    repo = git_source(tmp_path / "found", [("1.0", {}), ("1.1", {}), ("1.2", {})])
    mod = make_mod(tmp_path / "mod")
    S.add_source(mod, repo, FOUNDATION, vanilla_repo=vanilla)
    S.assign_patch(mod, "found", "1.0..1.1", "1.0.0", vanilla)
    src = S.load_sources(mod).by_id("found")
    assert [p for _c, _t, p in src.versions()] == ["1.0.0", "1.0.0", "1.1.0"]
    assert S.key_info(src.key)["patches"]["1.0"]["how"] == "chosen"
    S.assign_patch(mod, "found", "1.1..1.2", "1.1.0", vanilla)
    with pytest.raises(SourceError, match="1.1 has patch 1.1.0"):
        S.assign_patch(mod, "found", "1.2", "1.0.0", vanilla)
    with pytest.raises(SourceError, match="not a tracked vanilla version"):
        S.assign_patch(mod, "found", "1.0", "9.9", vanilla)


def test_a_decreasing_patch_is_refused_naming_the_version(tmp_path, data, vanilla):
    repo = git_source(tmp_path / "found", [("1.0", {}), ("1.1", {})])
    mod = make_mod(tmp_path / "mod")
    S.add_source(mod, repo, FOUNDATION, vanilla_repo=vanilla)
    S.assign_patch(mod, "found", "1.0", "1.1.0", vanilla)
    with pytest.raises(SourceError, match="1.0 has patch 1.1.0"):
        S.assign_patch(mod, "found", "1.1", "1.0.0", vanilla)


def test_an_unknown_previous_patch_leaves_the_new_version_unpatched(tmp_path, data, vanilla):
    repo = git_source(tmp_path / "found", [("1.0", {})])
    mod = make_mod(tmp_path / "mod")
    S.add_source(mod, repo, FOUNDATION, vanilla_repo=vanilla)
    src = S.load_sources(mod).by_id("found")
    info = S.key_info(src.key)
    info["patches"]["1.0"] = {"patch": "0.9.0", "how": "chosen"}
    S.save_key_info(src.key, info)
    git_source(repo, [("1.1", {})])
    warnings = S.record_versions(src, vanilla)
    assert src.versions()[-1][2] is None
    assert "0.9.0 is not in the vanilla tracker" in warnings[0]


def test_a_default_while_the_tracker_lags_is_warned_and_marked(tmp_path, data, vanilla):
    repo = git_source(tmp_path / "found", [("1.0", {})])
    mod = make_mod(tmp_path / "mod")
    S.add_source(mod, repo, FOUNDATION, vanilla_repo=vanilla)
    src = S.load_sources(mod).by_id("found")
    git_source(repo, [("1.1", {})])
    warnings = S.record_versions(src, vanilla, stale=True)
    assert "out of date" in warnings[0]
    view = S.sources_view(mod, vanilla, refresh=False)
    assert "defaulted while the tracker lagged" in S.render_sources(view)


# --- folder snapshots and the shared tracker ---------------------------------------------------

def folder_source(root, version="2.0", files=None):
    _write_tree(root, {".metadata/metadata.json": meta("fold", version),
                       **(files or {"in_game/common/x/f.txt": "f = { a = 1 }\n"})})
    return root


def test_a_folder_is_snapshotted_only_on_request(tmp_path, data, vanilla):
    fold = folder_source(tmp_path / "fold")
    mod = make_mod(tmp_path / "mod")
    messages = S.add_source(mod, fold, FOUNDATION, vanilla_repo=vanilla)
    src = S.load_sources(mod).by_id("fold")
    assert src.versions() == [] and "--snapshot-source fold" in messages[-1]
    assert not S.shared_repo().exists()

    S.snapshot_source(mod, "fold", vanilla)
    refs = T.git(S.shared_repo(), "for-each-ref", "--format=%(refname)").split()
    assert refs == [f"refs/sources/{src.key}/head", f"refs/sources/{src.key}/tags/2.0"]
    assert [(t, p) for _c, t, p in src.versions()] == [("2.0", "1.1.0")]

    assert "nothing committed" in S.snapshot_source(mod, "fold", vanilla)[0]
    (fold / "in_game/common/x/f.txt").write_text("f = { a = 2 }\n")
    S.snapshot_source(mod, "fold", vanilla)
    folder_source(fold, "2.1")
    S.snapshot_source(mod, "fold", vanilla)
    assert [t for _c, t, _p in src.versions()] == ["2.0", "2.0.2", "2.1"]


def test_identical_files_are_stored_once_across_sources(tmp_path, data, vanilla):
    a = folder_source(tmp_path / "a")
    b = folder_source(tmp_path / "b")
    _write_tree(b, {".metadata/metadata.json": meta("other", "2.0")})
    mod = make_mod(tmp_path / "mod")
    S.add_source(mod, a, FOUNDATION, vanilla_repo=vanilla)
    S.add_source(mod, b, ADOPTED, vanilla_repo=vanilla)
    S.snapshot_source(mod, "fold", vanilla)
    S.snapshot_source(mod, "other", vanilla)
    blob = lambda sid: T.git(S.shared_repo(), "rev-parse",
                             f"refs/sources/{S.load_sources(mod).by_id(sid).key}/head:in_game/common/x/f.txt").strip()
    assert blob("fold") == blob("other")


def test_a_folder_shipping_git_imports_its_history_on_the_first_snapshot(tmp_path, data, vanilla):
    item = tmp_path / "Steam/steamapps/workshop/content/123/456"
    git_source(item, [("1.0", {"in_game/a.txt": "1"}), ("1.1", {"in_game/a.txt": "2"})], mod_id="ws")
    (item / "in_game/a.txt").write_text("3")                  # Steam replaced a file without a commit
    mod = make_mod(tmp_path / "mod")
    S.add_source(mod, item, FOUNDATION, vanilla_repo=vanilla)
    messages = S.snapshot_source(mod, "ws", vanilla)
    assert "Imported" in messages[0]
    src = S.load_sources(mod).by_id("ws")
    versions = src.versions()
    assert [t for _c, t, _p in versions][:2] == ["1.0", "1.1"] and versions[-1][1] == "1.1.2"
    assert [p for _c, _t, p in versions] == [None, None, "1.1.0"]      # imported history has no patch


def test_the_tracker_lock_blocks_a_second_writer_and_clears_a_stale_lock(tmp_path, data):
    S.lock_path().parent.mkdir(parents=True, exist_ok=True)
    live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        S.lock_path().write_text(json.dumps({"pid": live.pid, "start": S._process_start(live.pid)}))
        with pytest.raises(SourceError, match=str(live.pid)):
            with S.tracker_lock(timeout=0.3, poll=0.05):
                pass
    finally:
        live.kill()
        live.wait()
    with S.tracker_lock(timeout=0.3, poll=0.05):
        assert json.loads(S.lock_path().read_text())["pid"] == os.getpid()
    assert not S.lock_path().exists()


# --- orphaned sources ------------------------------------------------------------------------

def test_orphans_are_listed_confirmed_and_removed_through_refs_and_files(tmp_path, data, vanilla, monkeypatch):
    fold = folder_source(tmp_path / "fold")
    mod = make_mod(tmp_path / "mod")
    S.add_source(mod, fold, FOUNDATION, vanilla_repo=vanilla)
    S.snapshot_source(mod, "fold", vanilla)
    key = S.load_sources(mod).by_id("fold").key
    cache = S.source_cache_dir() / f"{key}-vocab-v1-{'0' * 40}.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text("{}")
    loose = subprocess.run(["git", "--git-dir", str(S.shared_repo()), "hash-object", "-w", "--stdin"],
                           input="written before its refs", capture_output=True, text=True).stdout.strip()
    assert S.orphaned_sources() == []

    S.remove_source(mod, "fold")
    assert S.orphaned_sources() == [key] and key in S.orphan_sources_note()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert S.remove_orphaned_sources() == 1 and key in S.registry()

    assert S.remove_orphaned_sources(force=True) == 0
    assert key not in S.registry()
    assert T.git(S.shared_repo(), "for-each-ref", f"refs/sources/{key}/").strip() == ""
    assert not S.key_info_path(key).exists() and not cache.exists()
    assert subprocess.run(["git", "--git-dir", str(S.shared_repo()), "cat-file", "-e", loose]).returncode == 0
    assert S.shared_repo().is_dir()


# --- suggestions and the scan cache ------------------------------------------------------------

@pytest.fixture
def machine(tmp_path, monkeypatch, data):
    """A Steam install with the game and two libraries of workshop items, and the
    game's user folder with a playset and local mods."""
    steam = tmp_path / "Steam"
    lib2 = tmp_path / "Lib2"
    game = steam / "steamapps/common/Game/game"
    game.mkdir(parents=True)
    monkeypatch.setenv("PDX_GAME_ROOT", str(game))
    _write_tree(steam, {
        "steamapps/appmanifest_123.acf": '"AppState"\n{\n\t"appid"\t\t"123"\n\t"installdir"\t\t"Game"\n}\n',
        "steamapps/libraryfolders.vdf": ('"libraryfolders"\n{\n\t"0"\n\t{\n\t\t"path"\t\t"%s"\n\t}\n'
                                         '\t"1"\n\t{\n\t\t"path"\t\t"%s"\n\t}\n}\n' % (steam, lib2)),
        "steamapps/workshop/appworkshop_123.acf": '"AppWorkshop"\n{\n"WorkshopItemsInstalled"\n{\n'
                                                  '"900"\n{\n"manifest"\t"m1"\n}\n}\n}\n',
        "steamapps/workshop/content/123/900/.metadata/metadata.json": meta("found", "2.0"),
    })
    _write_tree(lib2, {
        "steamapps/workshop/appworkshop_123.acf": '"AppWorkshop"\n{\n}\n',
        "steamapps/workshop/content/123/901/.metadata/metadata.json": meta("found", "2.0"),
        "steamapps/workshop/content/123/902/.metadata/metadata.json": meta("unrelated", "1.0"),
    })
    user = tmp_path / "Game"
    mod = make_mod(user / "mod/mine", deps=("found",))
    git_source(user / "mod/found-dev", [("2.1", {})], mod_id="found.dev")
    _write_tree(user, {"playsets.json": json.dumps({"playsets": [{"orderedListMods": [
        {"path": str(user / "mod/mine") + "/", "isEnabled": True}]}]})})
    return {"steam": steam, "lib2": lib2, "user": user, "mod": mod, "game": game}


def test_suggestions_for_declared_dependencies_same_ids_and_a_dev_clone(machine, vanilla):
    mod = machine["mod"]
    scan = S.refresh_scan(mod)
    found = sorted((s["reason"], Path(s["path"]).name) for s in S.suggestions(mod, scan))
    assert found == [("dependency", "900"), ("dependency", "901"), ("git", "found-dev")]
    assert S.load_sources(mod).foundations == []            # nothing is added on its own


def test_an_ignored_suggestion_returns_when_declared_dependencies_change(machine, vanilla):
    mod = machine["mod"]
    S.ignore_suggestion(mod, "found")
    S.ignore_suggestion(mod, "found.dev")
    scan = S.refresh_scan(mod)
    assert S.suggestions(mod, scan) == [] and S.suggestion_note(mod) is None
    _write_tree(mod, {".metadata/metadata.json": meta("my_mod", "1.0", ("found", "unrelated"))})
    assert {s["id"] for s in S.suggestions(mod, S.refresh_scan(mod))} == {"found", "unrelated", "found.dev"}


def test_a_run_reads_the_cached_scan_and_never_scans(machine, monkeypatch):
    mod = machine["mod"]
    assert "run `pdx-audit --sources`" in S.suggestion_note(mod)
    S.refresh_scan(mod)
    monkeypatch.setattr(S, "read_metadata", lambda root: (_ for _ in ()).throw(AssertionError("scanned")))
    monkeypatch.setattr(S, "declared_dependencies", lambda root: ["found"])
    note = S.suggestion_note(mod)
    assert "900" in note and "901" in note and "--add-source" in note


@pytest.mark.parametrize("change", ["library", "workshop", "mod_folder", "metadata_id", "gains_metadata",
                                    "playset"])
def test_each_stamp_invalidates_the_scan_cache(machine, change):
    mod, user = machine["mod"], machine["user"]
    bare = user / "mod/bare"
    bare.mkdir()
    S.refresh_scan(mod)
    assert S.cached_scan(mod) is not None
    time.sleep(0.01)
    if change == "library":
        vdf = machine["steam"] / "steamapps/libraryfolders.vdf"
        vdf.write_text(vdf.read_text().replace('"1"', '"2"\n\t{\n\t}\n\t"1"'))
    elif change == "workshop":
        acf = machine["lib2"] / "steamapps/workshop/appworkshop_123.acf"
        acf.write_text('"AppWorkshop"\n{\n"WorkshopItemsInstalled"\n{\n}\n}\n')
    elif change == "mod_folder":
        (user / "mod/new").mkdir()
    elif change == "metadata_id":
        _write_tree(user / "mod/found-dev", {".metadata/metadata.json": meta("found.dev2", "2.1")})
    elif change == "gains_metadata":
        _write_tree(bare, {".metadata/metadata.json": meta("bare", "1.0")})
    else:
        (user / "playsets.json").write_text(json.dumps({"playsets": []}))
    assert S.cached_scan(mod) is None


# --- fixes from a test user's pass ------------------------------------------------------------

def test_the_working_tree_check_never_writes_into_the_repository(tmp_path, data, vanilla):
    repo = git_source(tmp_path / "found", [("1.0", {"in_game/a.txt": "1\n"})])
    mod = make_mod(tmp_path / "mod")
    S.add_source(mod, repo, FOUNDATION, vanilla_repo=vanilla)
    index = repo / ".git" / "index"
    os.utime(repo / "in_game/a.txt", ns=(10**9, 10**9))          # stat data git would refresh
    before = (index.stat().st_mtime_ns, index.read_bytes())
    src = S.load_sources(mod).by_id("found")
    assert S.freshness(src, full=True) == []
    S.render_sources(S.sources_view(mod, vanilla, refresh=False, full=True))
    assert (index.stat().st_mtime_ns, index.read_bytes()) == before
    (repo / "in_game/a.txt").write_text("2\n")
    assert "not committed" in S.freshness(src, full=True)[0]


def test_a_folder_differing_only_in_line_endings_is_not_snapshotted_again(tmp_path, data, vanilla):
    item = tmp_path / "Steam/steamapps/workshop/content/123/456"
    git_source(item, [("1.0", {"in_game/a.txt": "a = {\n\tb = 1\n}\n"})], mod_id="ws")
    (item / "in_game/a.txt").write_bytes(b"a = {\r\n\tb = 1\r\n}\r\n")
    mod = make_mod(tmp_path / "mod")
    S.add_source(mod, item, FOUNDATION, vanilla_repo=vanilla)
    assert "nothing committed" in " ".join(S.snapshot_source(mod, "ws", vanilla))
    assert [t for _c, t, _p in S.load_sources(mod).by_id("ws").versions()] == ["1.0"]


def test_relocation_refuses_a_folder_holding_another_source(tmp_path, data, vanilla):
    mod = make_mod(tmp_path / "mod")
    S.add_source(mod, git_source(tmp_path / "found", [("1.0", {})]), FOUNDATION, vanilla_repo=vanilla)
    with pytest.raises(SourceError, match="holds other"):
        S.relocate_source(mod, "found", git_source(tmp_path / "other", [("1.0", {})], mod_id="other"))


def test_a_source_switched_to_git_keeps_its_older_history_unpatched(tmp_path, data, vanilla):
    repo = git_source(tmp_path / "found", [("1.0", {}), ("1.1", {})])
    mod = make_mod(tmp_path / "mod")
    S.add_source(mod, repo, FOUNDATION, kind=FOLDER, vanilla_repo=vanilla)
    S.set_kind(mod, "found", "auto")
    src = S.load_sources(mod).by_id("found")
    S.record_versions(src, vanilla)
    assert [(t, p) for _c, t, p in src.versions()] == [("1.0", None), ("1.1", "1.1.0")]


def test_adopted_sources_take_no_patches_and_rule_changes_say_so(tmp_path, data, vanilla):
    mod = make_mod(tmp_path / "mod")
    S.add_source(mod, git_source(tmp_path / "g", [("1.0", {})], mod_id="g"), ADOPTED, vanilla_repo=vanilla)
    with pytest.raises(SourceError, match="adopted source"):
        S.assign_patch(mod, "g", "1.0", "1.0.0", vanilla)
    S.add_rename(mod, "g", "old_", "new_")
    assert "replacing the rule old_ → new_" in S.add_rename(mod, "g", "old_", "other_")[0]
    assert S.ignore_suggestion(mod, "bogus")[-1].startswith("Note:")


# --- freshness and caches -------------------------------------------------------------------

def test_freshness_of_folder_and_git_sources(machine, vanilla, tmp_path):
    mod = machine["mod"]
    item = machine["steam"] / "steamapps/workshop/content/123/900"
    _write_tree(item, {"in_game/common/x/f.txt": "f = 1\n"})
    S.add_source(mod, item, FOUNDATION, vanilla_repo=vanilla)
    S.snapshot_source(mod, "found", vanilla)
    src = S.load_sources(mod).by_id("found")
    assert S.freshness(src, full=True) == []

    (item / "in_game/common/x/f.txt").write_bytes(b"f = 1\r\n")          # CR only
    assert S.freshness(src, full=True) == []
    (item / "in_game/common/x/f.txt").write_text("f = 2\n")
    assert "changed after its snapshot" in S.freshness(src, full=True)[0]
    assert S.freshness(src) == []                                          # the quick check reads no files
    acf = machine["steam"] / "steamapps/workshop/appworkshop_123.acf"
    acf.write_text(acf.read_text().replace("m1", "m2"))
    assert "Steam updated found" in S.freshness(src)[0]

    repo = git_source(tmp_path / "g", [("1.0", {"in_game/a.txt": "1\n"})], mod_id="g")
    S.add_source(mod, repo, ADOPTED, vanilla_repo=vanilla)
    g = S.load_sources(mod).by_id("g")
    assert S.freshness(g, full=True) == []
    (repo / "in_game/a.txt").write_bytes(b"1\r\n")
    assert S.freshness(g, full=True) == []
    git_source(repo, [("1.1", {})], mod_id="g")
    assert "HEAD moved past" in S.freshness(g)[0]


def test_cache_pruning_runs_per_source(tmp_path, data, vanilla):
    fold = folder_source(tmp_path / "fold")
    mod = make_mod(tmp_path / "mod")
    S.add_source(mod, fold, FOUNDATION, vanilla_repo=vanilla)
    S.snapshot_source(mod, "fold", vanilla)
    src = S.load_sources(mod).by_id("fold")
    live = src.raw_versions()[0][0]
    folder = S.source_cache_dir()
    folder.mkdir(parents=True, exist_ok=True)
    keep, drop = folder / f"{src.key}-vocab-v1-{live}.json", folder / f"{src.key}-vocab-v1-{'1' * 40}.json"
    other = folder / f"other-abcdef-vocab-v1-{'1' * 40}.json"
    for fp in (keep, drop, other):
        fp.write_text("{}")
    T.prune_cache(src)
    assert keep.exists() and not drop.exists() and other.exists()
    assert T.cache_path(src, "x.json") == folder / f"{src.key}-x.json"
    assert T.cache_path(vanilla, "x.json") == Path(vanilla).parent / "cache" / "x.json"
