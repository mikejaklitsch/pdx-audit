"""The desktop app's payload: the override audit attaches a block payload to its
findings only when a run writes a results file, and build_payload turns findings
into the records the app lists."""
import io
from contextlib import redirect_stdout

from conftest import audit_args
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


def test_results_file_attaches_one_block_to_every_finding_of_a_target(world):
    findings = _run(run_override_audit, world.mod, world.repo,
                    world.old, "1.0.0", world.new, "1.1.0", audit_args(results_file="r.json"))
    blocks = {id(f.data): f.data for f in findings if f.name == "some_building"}
    assert len(blocks) == 1
    [block] = blocks.values()
    assert block["type"] == "REPLACE" and block["lines"][0] == "REPLACE:some_building = {"
    assert {c["kind"] for c in block["changes"]} == {"vanilla_added", "vanilla_removed"}


BLOCK = {"type": "REPLACE", "file": "m.txt", "line": 1, "vanilla_file": "v.txt",
         "lines": ["some_building = {", "}"], "changes": []}


def _finding(kind="override_vanilla_added_mid", name="some_building", vanilla="upkeep = 5", data=BLOCK):
    return Finding(kind, name, "m.txt:1", "", data,
                   {"target": f"override:x/{name}", "path": [], "yours": None, "vanilla": vanilla}, "1.1.0")


def test_payload_records_carry_ids_and_block():
    payload = build_payload([_finding()], mod_name="testmod", old_msg="1.0.0", new_msg="1.1.0",
                            new_tag="1.1.0")
    rec = payload["records"][0]
    assert rec["name"] == "some_building" and rec["file"] == "m.txt" and rec["line"] == "1"
    assert rec["id"] == rec["fid"][:8] and len(rec["fid"]) == 40
    assert rec["since"] == "1.1.0" and rec["earlier"] is False
    assert payload["blocks"][rec["block"]]["lines"]


def test_payload_shares_block_data_across_its_findings():
    recs = build_payload([_finding(), _finding(vanilla="cost = 2")], new_tag="1.1.0")["records"]
    assert len(recs) == 2 and recs[0]["block"] == recs[1]["block"]


def test_payload_skips_informational_findings_but_counts_them():
    payload = build_payload([Finding("override_inject_context", "quiet_block", "m.txt:9")])
    assert payload["records"] == [] and payload["info"] == 1


def test_payload_marks_findings_from_earlier_patches():
    fs = [Finding("loc_removed", "KEY", "m.yml", "english", None, {"target": "loc:KEY"},
                  "1.0.0")]
    assert build_payload(fs, new_tag="1.1.0")["records"][0]["earlier"] is True


def test_payload_counts_dismissed():
    payload = build_payload([], dismissed=3, new_tag="1.1.0")
    assert payload["dismissed"] == 3 and payload["new_tag"] == "1.1.0"
