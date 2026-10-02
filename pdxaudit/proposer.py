"""Proposals: rule and entry candidates that the user confirms.

The proposer groups the deviations that no rule and no recorded entry explains, and
drafts one candidate for each group: a rule for a group of two or more, an entry for
one deviation. It collects evidence for each candidate: the pdx-maint systems of the
file, the mod commits that wrote the lines, the comments at the lines, and the
pdx-maint notes that name the block. It fills `reason` only from a pdx-maint note,
with the note id as the source. It never fills a reason from a commit message or a
code comment, and it never chooses a disposition.

A seed turns records that the user already gave into candidates: dismissals with a
reason, the lines of a keep file (`<block>.<path>  <reason>`), and a rules file.

A proposal is a JSON file in the per-user data folder. Nothing becomes a rule or an
entry until `intent accept` reads it."""
import datetime
import json
import os
import re
import subprocess
from collections import defaultdict
from pathlib import Path

from . import intent, session

PROPOSAL_KIND = "pdx-audit proposal"


# --- evidence -----------------------------------------------------------------------

def notes_dir(mod_root):
    """The folder of the mod's pdx-maint notes, or None. pdx-maint prints the path of a
    note file; the notes sit together in that folder."""
    def find():
        for nid in ("1", "0001"):
            try:
                r = subprocess.run(["pdx-maint", "note", "path", nid], cwd=str(mod_root),
                                   capture_output=True, text=True, timeout=30)
            except (OSError, subprocess.TimeoutExpired):
                return None
            p = Path(r.stdout.strip())
            if r.returncode == 0 and p.parent.is_dir():
                return p.parent
        return None
    return session.memo(("notes_dir", str(mod_root)), find)


def read_notes(folder):
    """[{id, title, status, body}] for the note files in `folder`."""
    if folder is None:
        return []
    out = []
    for p in sorted(Path(folder).glob("*.md")):
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        meta, body = {}, text
        if text.startswith("---"):
            head, _sep, body = text[3:].partition("\n---")
            for line in head.split("\n"):
                k, sep, v = line.partition(":")
                if sep:
                    meta.setdefault(k.strip(), v.strip())
        if meta.get("status") == "superseded":
            continue
        out.append({"id": meta.get("id", p.stem.lstrip("0")), "title": meta.get("title", ""),
                    "body": body})
    return out


def note_lines(notes, name):
    """[(note id, line)] for the note lines that name `name` as a whole word."""
    if not name:
        return []
    rx = re.compile(rf"(?<![\w]){re.escape(name)}(?![\w])")
    hits = []
    for n in notes:
        for line in n["body"].split("\n"):
            if rx.search(line):
                hits.append((n["id"], line.strip().lstrip("-* ").strip()))
    return hits


def _ranges(lines, gap=3):
    """`-L a,b` ranges that cover the line numbers, joining runs closer than `gap`."""
    out = []
    for ln in sorted(set(lines)):
        if out and ln - out[-1][1] <= gap:
            out[-1][1] = ln
        else:
            out.append([ln, ln])
    return out


def _blame_file(mod_root, rel, lines):
    """{line: (commit, subject)} for some lines of a working-tree file, from one
    `git blame -C -C` call with one -L range for each run of lines. -C -C follows
    lines that moved or were copied between files in the commit that created them;
    it does not search every file of every commit."""
    if not lines:
        return {}
    cmd = ["git", "-C", str(mod_root), "blame", "-C", "-C", "--line-porcelain"]
    for a, b in _ranges(lines):
        cmd += ["-L", f"{a},{b}"]
    try:
        r = subprocess.run(cmd + ["--", rel], capture_output=True, text=True, timeout=600,
                           encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if r.returncode != 0:
        return {}
    out, commit, final, subject = {}, None, None, ""
    for line in r.stdout.split("\n"):
        if re.match(r"^[0-9a-f]{40} ", line):
            parts = line.split()
            commit, final = parts[0], int(parts[2])
        elif line.startswith("summary "):
            subject = line[8:]
        elif line.startswith("\t") and commit:
            out[final] = (commit[:10], subject)
    return out


def blame_many(mod_root, wanted, workers=4):
    """{rel: {line: (commit, subject)}} for {rel: lines}. One blame per file, in a
    thread pool, because the cost is waiting on the disk. Each answer is kept for the
    run, keyed by the file's text, so an unchanged file is not blamed again."""
    from concurrent.futures import ThreadPoolExecutor
    out, todo = {}, {}
    for rel, lines in wanted.items():
        try:
            key = ("blame", str(mod_root), rel, hash(session.read_text(Path(mod_root) / rel)))
        except (OSError, UnicodeDecodeError):
            continue
        have = session.memo(key, dict)
        missing = set(lines) - set(have)
        if missing:
            todo[rel] = (have, missing)
        out[rel] = have
    with ThreadPoolExecutor(max(1, min(workers, len(todo) or 1))) as pool:
        futures = {rel: pool.submit(_blame_file, mod_root, rel, missing) for rel, (_h, missing) in todo.items()}
        for rel, fut in futures.items():
            have, missing = todo[rel]
            got = fut.result()
            for ln in missing:
                have[ln] = got.get(ln)
    return out


def blame(mod_root, rel, lines=()):
    """{line: (commit, subject)} for the lines asked; see blame_many."""
    return blame_many(mod_root, {rel: lines}).get(rel, {})


def evidence(blamed, devs, notes):
    commits, comments, systems, lines = {}, [], set(), []
    for d in devs:
        systems.update(d.systems)
        hit = blamed.get(d.file, {}).get(d.line) if blamed else None
        if hit and hit[0] not in commits and not hit[0].startswith("0000000000"):
            commits[hit[0]] = hit[1]
        if d.comment and d.comment not in comments:
            comments.append(d.comment)
    for block in sorted({d.block for d in devs}):
        lines += note_lines(notes, block)
    return {"systems": sorted(systems), "commits": [{"commit": c, "subject": s} for c, s in list(commits.items())[:5]],
            "comments": comments[:5], "notes": [{"note": n, "line": t} for n, t in lines[:5]]}


# --- proposals ----------------------------------------------------------------------

def _prefill(ev):
    """(reason, source) from the first note line, or ("", user source)."""
    if ev["notes"]:
        first = ev["notes"][0]
        return first["line"], {"kind": "note", "ref": str(first["note"])}
    return "", {"kind": "user"}


def _members(devs):
    return [d.to_json() for d in devs]


def propose(mod_root, copies, devs, state, notes, today=None, system=None):
    """A proposal for the deviations that nothing explains."""
    today = today or datetime.date.today().isoformat()
    it = intent.of(state)
    states = intent.entry_states(it, copies, devs)
    open_devs = [d for d in devs if not d.generated and intent.attribute(d, it, states) is None]
    if system:
        open_devs = [d for d in open_devs if system in d.systems]
    groups = defaultdict(list)
    for d in open_devs:
        key = d.vanilla_key or d.mod_key
        groups[((d.systems or ["?"])[0], d.content, d.change, key)].append(d)
    candidates = []
    pool = [d for d in devs if not d.generated]
    wanted = defaultdict(set)
    for d in open_devs:
        wanted[d.file].add(d.line)
    blamed = blame_many(mod_root, wanted) if mod_root else {}
    for n, ((sys_id, content, change, key), members) in enumerate(sorted(groups.items(), key=lambda kv: (
            kv[0][0], kv[0][1], kv[0][2], kv[0][3] or "")), 1):
        ev = evidence(blamed, members, notes)
        reason, source = _prefill(ev)
        if len(members) > 1:
            side = "vanilla_key" if members[0].vanilla_key is not None else "mod_key"
            match = {"content": [content], "change": [change]}
            if key:
                match[side] = [key]
            record = {"id": f"{sys_id if sys_id != '?' else 'system'}.{key or 'node'}_{change}",
                      "system": sys_id if sys_id != "?" else "", "reason": reason, "source": source,
                      "disposition": "", "match": match, "tool": None, "created": today,
                      "confirmed_by": "user"}
            selects = sum(1 for d in pool if intent.rule_matches(record, d))
            candidates.append({"cid": f"c{n}", "type": "rule", "record": record, "evidence": ev,
                               "selects": selects, "members": _members(members)})
        else:
            d = members[0]
            record = intent.entry_from(d, f"e-{d.id[:8]}", sys_id if sys_id != "?" else "", reason, source,
                                       "", scope="node", today=today)
            record["seen"] = {"vanilla": d.since, "mod_sig": intent.digest(d.mod_text),
                              "vanilla_sig": intent.digest(d.vanilla_text)}
            intent.seal(record, copies, devs)
            candidates.append({"cid": f"c{n}", "type": "entry", "record": record, "evidence": ev,
                               "selects": 1, "members": _members(members)})
    return candidates


def _common_prefix(paths):
    if not paths:
        return []
    out = []
    for segs in zip(*paths):
        if all(s == segs[0] for s in segs):
            out.append(segs[0])
        else:
            break
    return out


def seed_dismissals(copies, devs, state, today=None):
    """Entry candidates from the dismissals that carry a reason."""
    today = today or datetime.date.today().isoformat()
    by_finding = defaultdict(list)
    for d in devs:
        if d.finding:
            by_finding[d.finding].append(d)
    out = []
    for fid, e in sorted(state.get("dismissed", {}).items()):
        reason = (e.get("reason") or "").strip()
        hit = by_finding.get(fid)
        if not reason or not hit:
            continue
        path = _common_prefix([d.path for d in hit])
        scope = "node" if len(hit) == 1 and path == hit[0].path else "subtree"
        d = hit[0]
        record = intent.entry_from(d, f"e-{fid[:8]}", (d.systems or [""])[0], reason,
                                   {"kind": "user", "ref": f"dismissal {fid[:8]}"}, "keep_mod",
                                   scope=scope, today=today)
        record["address"]["path"] = path
        intent.seal(record, copies, devs)
        out.append({"type": "entry", "record": record, "evidence": {"dismissed": e},
                    "selects": len(hit), "members": _members(hit)})
    return out


KEEP_LINE = re.compile(r"^\s*([\w.:@-]+)\s+(.+?)\s*$")


def seed_keep_file(copies, devs, path, today=None):
    """Entry candidates from a keep file: `<block>.<path>  <reason>` on each line."""
    today = today or datetime.date.today().isoformat()
    out, unmatched = [], []
    for raw in Path(path).read_text(encoding="utf-8-sig").split("\n"):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        m = KEEP_LINE.match(raw)
        if not m:
            continue
        block, *keys = m.group(1).split(".")
        hit = [d for d in devs if d.block == block and list(d.keys[:len(keys)]) == keys]
        if not hit:
            unmatched.append(m.group(1))
            continue
        d = hit[0]
        record = intent.entry_from(d, f"e-keep-{block}-{'-'.join(keys)}", (d.systems or [""])[0],
                                   m.group(2), {"kind": "user", "ref": str(path)}, "keep_mod",
                                   scope="subtree", depth=len(keys), today=today)
        intent.seal(record, copies, devs)
        out.append({"type": "entry", "record": record, "evidence": {"line": raw.strip()},
                    "selects": len(hit), "members": _members(hit)})
    return out, unmatched


def seed_rules(devs, path, notes, today=None):
    """Rule candidates from a rules file: a JSON list of rules. A rule whose
    `source` is a note and whose reason is empty takes the note line that holds
    `reason_from`, when the file gives it."""
    today = today or datetime.date.today().isoformat()
    pool = [d for d in devs if not d.generated]
    out = []
    for rule in json.loads(Path(path).read_text(encoding="utf-8")):
        rule = dict(rule)
        hint = rule.pop("reason_from", None)
        src = rule.get("source") or {}
        if not rule.get("reason") and hint and src.get("kind") == "note":
            note = next((n for n in notes if str(n["id"]) == str(src.get("ref"))), None)
            line = next((t for _i, t in note_lines([note], hint)), None) if note else None
            if line:
                rule["reason"] = line
        rule.setdefault("created", today)
        rule.setdefault("confirmed_by", "user")
        members = [d for d in pool if intent.rule_matches(rule, d)]
        out.append({"type": "rule", "record": rule, "evidence": {}, "selects": len(members),
                    "members": _members(members[:50])})
    return out


def write(candidates, store_dir, mod_id, vanilla, out=None):
    """Write a proposal file and return its path. The candidates get ids c1, c2, ..."""
    for n, c in enumerate(candidates, 1):
        c["cid"] = f"c{n}"
    doc = {"kind": PROPOSAL_KIND, "version": 1, "mod": mod_id, "vanilla": vanilla,
           "created": datetime.datetime.now().isoformat(timespec="seconds"), "candidates": candidates}
    if out is None:
        folder = Path(store_dir) / "proposals"
        folder.mkdir(parents=True, exist_ok=True)
        out = folder / f"{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    out = Path(out)
    tmp = out.with_name(f".{out.name}.tmp-{os.getpid()}")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, out)
    return out


def accept(state, proposal_path, only=None, reg=None):
    """Add the candidates of a proposal to the store. Returns (added, errors). A
    candidate needs a reason, a system and, for an entry, a disposition. A rule with
    the disposition null is a grouping; an empty string means "not chosen yet"."""
    doc = json.loads(Path(proposal_path).read_text(encoding="utf-8"))
    if doc.get("kind") != PROPOSAL_KIND:
        return [], [f"{proposal_path} is not a pdx-audit proposal"]
    wanted = set(only) if only else None
    added, errors = [], []
    for c in doc.get("candidates", []):
        if wanted is not None and c.get("cid") not in wanted:
            continue
        rec = c.get("record") or {}
        if rec.get("disposition") == "":
            errors.append(f"{c.get('cid')}: choose a disposition ({', '.join(intent.DISPOSITIONS)}, "
                          f"or null for a grouping rule)")
            continue
        try:
            if c.get("type") == "rule":
                intent.add_rule(state, rec, reg)
            else:
                intent.add_entry(state, rec, reg)
        except intent.IntentError as e:
            errors.append(f"{c.get('cid')}: {e}")
            continue
        added.append((c.get("cid"), rec["id"]))
    if wanted:
        missing = wanted - {c.get("cid") for c in doc.get("candidates", [])}
        errors += [f"{cid}: no such candidate" for cid in sorted(missing)]
    return added, errors
