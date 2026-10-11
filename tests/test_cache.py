"""Block-index cache and its self-cleaning. The vanilla block index is memoized
per commit hash so a plain run and a later --diff reuse the same extraction, and
prune_cache drops cache files whose commit has left the tracker."""
import io
from pathlib import Path
from contextlib import redirect_stdout

from pdxaudit.tracker import cache_dir_of, cache_key, prune_cache, git
from pdxaudit.overrides import run_override_audit


def _cache_dir(repo):
    return cache_dir_of(repo)


def _silent(fn, *a):
    with redirect_stdout(io.StringIO()):
        return fn(*a)


def test_block_index_is_cached_after_a_run(world):
    _silent(run_override_audit, world.mod, world.repo,
            world.old, "1.0.0", world.new, "1.1.0", world.args)
    files = list(_cache_dir(world.repo).glob("blocks-v*.json"))
    assert files, "expected a block-index cache file to be written"
    # a second run must reuse the cache and produce the same finding
    findings = _silent(run_override_audit, world.mod, world.repo,
                       world.old, "1.0.0", world.new, "1.1.0", world.args)
    assert any(f.name == "some_building" for f in findings)


def test_prune_cache_drops_orphans_keeps_live(world):
    d = _cache_dir(world.repo)
    d.mkdir(parents=True, exist_ok=True)
    live = git(world.repo, "rev-list", "--all").split()[0]
    keep = d / f"blocks-v1-{cache_key(world.repo, live)}-deadbeef1234.json"
    drop = d / f"vocab-v1-{'0' * 40}.json"
    keep.write_text("[]")
    drop.write_text("{}")
    prune_cache(world.repo)
    assert keep.exists(), "cache for a live commit must survive"
    assert not drop.exists(), "cache for a vanished commit must be pruned"


def test_clear_cache_deletes_only_the_cache_files(world, tmp_path):
    import os
    import subprocess
    import sys
    _silent(run_override_audit, world.mod, world.repo,
            world.old, "1.0.0", world.new, "1.1.0", world.args)
    d = _cache_dir(world.repo)
    written = sorted(d.glob("*.json"))
    assert written
    other = d / "notes.json"
    other.write_text("{}")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    env.pop("PDX_VANILLA_REPO", None)
    r = subprocess.run([sys.executable, "-m", "pdxaudit.cli", "--clear-cache", "--vanilla-repo", world.repo],
                       env=env, capture_output=True, text=True, encoding="utf-8",
                       cwd=Path(__file__).resolve().parent.parent)
    assert r.returncode == 0, r.stderr
    assert f"Deleted {len(written)} cache files" in r.stdout
    assert not any(p.exists() for p in written) and other.exists()
    # The next run reads each version again and gives the same finding.
    findings = _silent(run_override_audit, world.mod, world.repo,
                       world.old, "1.0.0", world.new, "1.1.0", world.args)
    assert any(f.name == "some_building" for f in findings)
