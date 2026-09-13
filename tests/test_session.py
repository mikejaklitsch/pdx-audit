"""The per-run memo: shared results inside a run, fresh reads outside one, and
tracker reads that match what `git archive` extraction produced."""
import io
import subprocess
import tarfile

from conftest import build_tracker
from pdxaudit import session
from pdxaudit.loc import build_loc_vanilla, parse_loc
from pdxaudit.tracker import MODULE_ROOTS


def _write(root, files):
    for rel, text in files.items():
        fp = root / rel
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(text, encoding="utf-8")


def test_mod_paths_lists_what_rglob_lists_in_the_module_roots(tmp_path):
    _write(tmp_path, {
        "in_game/common/a.txt": "", "in_game/.hidden.txt": "", "in_game/dir.txt/inner.txt": "",
        "in_game/gui/b.gui": "", "main_menu/localization/english/c_l_english.yml": "",
        "loading_screen/common/defines/d.txt": "", ".git/objects/e.txt": "", "docs/f.txt": "",
    })
    for suffix in ("", ".txt", ".gui", ".yml"):
        expected = sorted(p for p in tmp_path.rglob("*" + suffix)
                          if p.relative_to(tmp_path).parts[0] in MODULE_ROOTS
                          and len(p.relative_to(tmp_path).parts) > 1)
        assert session.mod_paths(tmp_path, suffix) == expected


def test_reads_are_shared_inside_a_run_and_fresh_outside(tmp_path):
    fp = tmp_path / "in_game" / "common" / "a.txt"
    _write(tmp_path, {"in_game/common/a.txt": "one"})
    with session.run():
        assert session.read_text(fp) == "one"
        fp.write_text("two", encoding="utf-8")
        assert session.read_text(fp) == "one"
    assert session.read_text(fp) == "two"
    fp.write_text("three", encoding="utf-8")
    assert session.read_text(fp) == "three"


LOC_OLD = {
    "in_game/localization/english/a_l_english.yml": 'l_english:\n KEY_A:0 "a from a"\n KEY_B:0 "b"\n',
    "in_game/localization/english/z_l_english.yml": 'l_english:\n KEY_A:0 "a from z"\n KEY_C:0 "c"\n',
    "main_menu/localization/french/a_l_french.yml": 'l_french:\n KEY_A:0 "fr"\n',
    "main_menu/localization/replace/english/r_l_english.yml": 'l_english:\n KEY_B:0 "b replaced"\n',
    "in_game/common/x.txt": "x = 1\n",
}
LOC_NEW = dict(LOC_OLD, **{
    "in_game/localization/english/z_l_english.yml": 'l_english:\n KEY_A:0 "a from z"\n KEY_C:0 "c2"\n',
})


def _archive_loc(repo, commit, wanted):
    """The localization reader as it was: extract every .yml with git archive."""
    langs = {lang for lang, _ in wanted}
    raw = subprocess.run(["git", f"--git-dir={repo}", "archive", "--format=tar", commit,
                          "--", "*.yml"], capture_output=True).stdout
    result = {}
    with tarfile.open(fileobj=io.BytesIO(raw), ignore_zeros=True) as tf:
        for m in tf.getmembers():
            if not m.isfile() or not m.name.endswith(".yml"):
                continue
            if langs and not any(f"_l_{lang}." in m.name or f"/{lang}/" in m.name for lang in langs):
                continue
            text = tf.extractfile(m).read().decode("utf-8-sig", errors="replace")
            for k, v in parse_loc(text).items():
                if k in wanted and k not in result:
                    result[k] = v
    return result


def test_loc_reader_matches_archive_extraction(tmp_path):
    t = build_tracker(tmp_path, [("1.0", LOC_OLD), ("1.1", LOC_NEW)])
    wanted_sets = [{("english", "KEY_A"), ("english", "KEY_B"), ("english", "KEY_C")},
                   {("english", "KEY_A"), ("french", "KEY_A")}]
    for wanted in wanted_sets:
        for commit in t.hashes.values():
            assert build_loc_vanilla(t.repo, commit, wanted) == _archive_loc(t.repo, commit, wanted)
    with session.run():   # the second snapshot reuses the files the first one parsed
        for wanted in wanted_sets:
            for commit in t.hashes.values():
                assert build_loc_vanilla(t.repo, commit, wanted) == _archive_loc(t.repo, commit, wanted)