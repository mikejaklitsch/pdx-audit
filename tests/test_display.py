"""The --display report: the override audit attaches a rich data payload to its
findings only under --display, and render_report turns findings into a
self-contained HTML page."""
import io
import re
import json
import types
from contextlib import redirect_stdout

from pdxaudit.overrides import run_override_audit
from pdxaudit.htmlreport import render_report
from pdxaudit.report import Finding


def _run(fn, *a):
    buf = io.StringIO()
    with redirect_stdout(buf):
        return fn(*a)


def test_no_data_without_display(world):
    findings = _run(run_override_audit, world.mod, world.repo,
                    world.old, "1.0.0", world.new, "1.1.0", world.args)
    assert findings and all(f.data is None for f in findings)


def test_display_attaches_report_data(world):
    args = types.SimpleNamespace(diff=False, block=None, category=None,
                                 full=True, old=None, new=None, display="r.html")
    findings = _run(run_override_audit, world.mod, world.repo,
                    world.old, "1.0.0", world.new, "1.1.0", args)
    f = next(x for x in findings if x.name == "some_building")
    assert f.data is not None
    assert any("upkeep = 5" in m for m in f.data["missing"])   # gap, with breadcrumb form
    assert f.data["patch"]                                     # 3-way block rows present


def test_render_report_is_self_contained():
    fs = [Finding("override_replace_stale", "some_building", "m.txt:1", "",
                  {"type": "REPLACE", "diff": "@@\n+\tupkeep = 5", "n_add": 1, "n_rem": 0,
                   "missing": ["upkeep = 5"], "kept": [], "overlap": [],
                   "patch": [{"t": "some_building = {", "c": "context"}],
                   "absent": [], "removed_note": []})]
    out = render_report(fs, "testmod", "1.0.0", "1.1.0")
    assert "some_building" in out and "upkeep = 5" in out and "testmod" in out
    assert "@@DATA@@" not in out and "@@MOD@@" not in out     # placeholders filled
    payload = json.loads(re.search(r'type="application/json">(.*?)</script>', out, re.S).group(1))
    assert payload["records"][0]["name"] == "some_building"


def test_render_report_skips_informational_findings():
    fs = [Finding("override_replace_reconciled", "quiet_block", "m.txt:9")]
    out = render_report(fs, "testmod", "1.0.0", "1.1.0")
    payload = json.loads(re.search(r'type="application/json">(.*?)</script>', out, re.S).group(1))
    assert payload["records"] == []          # info findings are not listed
