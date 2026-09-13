"""The desktop app's payload: the override audit attaches a rich data payload to
its findings only when a run writes a results file, and build_payload turns
findings into the records the app lists."""
import io
import types
from contextlib import redirect_stdout

from pdxaudit.overrides import run_override_audit
from pdxaudit.results import build_payload
from pdxaudit.report import Finding


def _run(fn, *a):
    buf = io.StringIO()
    with redirect_stdout(buf):
        return fn(*a)


def test_no_data_without_results_file(world):
    findings = _run(run_override_audit, world.mod, world.repo,
                    world.old, "1.0.0", world.new, "1.1.0", world.args)
    assert findings and all(f.data is None for f in findings)


def test_results_file_attaches_report_data_once_per_block(world):
    args = types.SimpleNamespace(diff=False, block=None, category=None,
                                 full=True, old=None, new=None, results_file="r.json")
    findings = _run(run_override_audit, world.mod, world.repo,
                    world.old, "1.0.0", world.new, "1.1.0", args)
    with_data = [x for x in findings if x.name == "some_building" and x.data]
    assert len(with_data) == 1
    data = with_data[0].data
    assert any("upkeep = 5" in m for m in data["missing"])
    assert any("legacy_mod = 1" in k for k in data["kept"])
    assert {line["c"] for line in data["lines"]} >= {"new_line", "kept_removed"}
    assert data["patch"]                                        # 3-way block rows present


BLOCK = {"type": "REPLACE", "diff": "@@\n+\tupkeep = 5", "n_add": 1, "n_rem": 0,
         "missing": ["upkeep = 5"], "kept": [], "overlap": [],
         "lines": [{"c": "new_line", "t": "upkeep = 5"}],
         "patch": [{"t": "some_building = {", "c": "context"}],
         "absent": [], "removed_note": []}


def test_payload_records_carry_ids_and_patch():
    fs = [Finding("override_replace_new_line", "some_building", "m.txt:1", "upkeep = 5",
                  BLOCK, {"target": "override:x/some_building"}, "1.1.0")]
    payload = build_payload(fs, mod_name="testmod", old_msg="1.0.0", new_msg="1.1.0",
                            new_tag="1.1.0")
    rec = payload["records"][0]
    assert rec["name"] == "some_building" and rec["file"] == "m.txt" and rec["line"] == "1"
    assert rec["id"] == rec["fid"][:8] and len(rec["fid"]) == 40
    assert rec["since"] == "1.1.0" and rec["earlier"] is False
    assert payload["blocks"][rec["block"]]["patch"]


def test_payload_shares_block_data_across_its_lines():
    fs = [Finding("override_replace_new_line", "b", "m.txt:1", "upkeep = 5", BLOCK,
                  {"target": "override:x/b", "slot": "upkeep"}, "1.1.0"),
          Finding("override_replace_kept_removed", "b", "m.txt:1", "legacy = 1", None,
                  {"target": "override:x/b", "slot": "legacy"}, "1.1.0")]
    recs = build_payload(fs, new_tag="1.1.0")["records"]
    assert len(recs) == 2 and recs[0]["block"] == recs[1]["block"]


def test_payload_skips_informational_findings_but_counts_them():
    fs = [Finding("override_replace_merged", "quiet_block", "m.txt:9")]
    payload = build_payload(fs)
    assert payload["records"] == [] and payload["info"] == 1


def test_payload_marks_findings_from_earlier_patches():
    fs = [Finding("loc_removed", "KEY", "m.yml", "english", None, {"target": "loc:KEY"},
                  "1.0.0")]
    assert build_payload(fs, new_tag="1.1.0")["records"][0]["earlier"] is True


def test_payload_counts_dismissed():
    payload = build_payload([], dismissed=3, new_tag="1.1.0")
    assert payload["dismissed"] == 3 and payload["new_tag"] == "1.1.0"
