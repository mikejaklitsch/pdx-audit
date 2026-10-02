"""`pdx-audit intent ...`: the commands of the intent store.

Each command reads the mod and writes only the per-user record and proposal files.
No intent command writes into the mod."""
import argparse
import datetime
import json
import sys

from . import intent, proposer, session
from .registry import registry


def _vanilla(mod_root, args):
    from .base import VanillaBase
    from .tracker import find_vanilla_repo, get_commits, tag_of
    repo = find_vanilla_repo(mod_root, args.vanilla_repo)
    commits = get_commits(repo)
    if not commits:
        print("Error: the tracker has no commits.", file=sys.stderr)
        sys.exit(1)
    if args.new:
        for i, (h, msg) in enumerate(commits):
            if tag_of(msg) == args.new or h.startswith(args.new) or args.new.startswith(h):
                return VanillaBase(repo), commits[i:], h, msg
        print(f"Error: --new {args.new} is not a tracked version.", file=sys.stderr)
        sys.exit(2)
    return VanillaBase(repo), commits, commits[0][0], commits[0][1]


def collect(mod_root, args):
    base, commits, new_hash, new_msg = _vanilla(mod_root, args)
    print("Reading the copies of the mod (this runs the copy audits)...", file=sys.stderr)
    copies, devs = intent.collect(mod_root, base, commits, new_hash, new_msg)
    from .tracker import tag_of
    return copies, devs, tag_of(new_msg)


def _source(text):
    kind, _sep, ref = (text or "user").partition(":")
    out = {"kind": kind}
    if ref:
        out["ref"] = ref
    return out


def build_parser():
    ap = argparse.ArgumentParser(prog="pdx-audit intent",
                                 description="Record why the mod differs from vanilla.")
    ap.add_argument("--mod-root")
    ap.add_argument("--vanilla-repo")
    ap.add_argument("--new", help="the vanilla version to compare with (default: the newest)")
    ap.add_argument("--json", action="store_true", help="print JSON")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list", help="list the rules and entries, with the state of each entry")
    p.add_argument("--system")
    p.add_argument("--state", choices=("recorded", "stale", "lost"))
    p = sub.add_parser("show", help="show one rule or entry")
    p.add_argument("id")
    p = sub.add_parser("add-rule", help="add the rules in a JSON file (one rule or a list)")
    p.add_argument("file")
    p = sub.add_parser("add", help="add an entry for the deviations of one audit finding")
    p.add_argument("--finding", required=True, help="the finding id, or its first characters")
    p.add_argument("--disposition", required=True, choices=intent.DISPOSITIONS)
    p.add_argument("--system", required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--source", default="user", help="note:<id>, commit:<sha> or user")
    p.add_argument("--scope", choices=intent.SCOPES, default="subtree")
    p = sub.add_parser("remove", help="remove a rule or an entry")
    p.add_argument("id")
    p = sub.add_parser("confirm", help="take a stale entry as the node reads now")
    p.add_argument("id")
    p = sub.add_parser("propose", help="write rule and entry candidates for the open deviations")
    p.add_argument("--system")
    p.add_argument("--out")
    p = sub.add_parser("seed", help="write candidates from records the user already gave")
    p.add_argument("--dismissals", action="store_true", help="dismissals that carry a reason")
    p.add_argument("--keep-file", help="a keep file: `<block>.<path>  <reason>` on each line")
    p.add_argument("--rules", help="a JSON file with a list of rules")
    p.add_argument("--out")
    p = sub.add_parser("accept", help="add the candidates of a proposal file to the store")
    p.add_argument("file")
    p.add_argument("--only", help="candidate ids, such as c1,c3")
    return ap


def main(argv):
    args = build_parser().parse_args(argv)
    with session.run():
        return _main(args)


def _main(args):
    from .store import open_store
    from .tracker import find_mod_root
    mod_root = find_mod_root(args.mod_root)
    store, err = open_store(mod_root)
    if store is None:
        print(f"Error: {err}", file=sys.stderr)
        return 1
    reg = registry(mod_root)
    it = intent.of(store.state)
    today = datetime.date.today().isoformat()

    if args.cmd == "show":
        rec = it["rules"].get(args.id) or it["entries"].get(args.id)
        if rec is None:
            print(f"Error: no rule or entry has id {args.id}", file=sys.stderr)
            return 1
        print(json.dumps(rec, indent=2, ensure_ascii=False))
        return 0

    if args.cmd == "remove":
        try:
            intent.remove(store.state, args.id)
        except intent.IntentError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        store.save()
        print(f"Removed {args.id}.")
        return 0

    if args.cmd == "add-rule":
        data = json.loads(open(args.file, encoding="utf-8").read())
        rules = data if isinstance(data, list) else [data]
        errors = 0
        for r in rules:
            r.setdefault("created", today)
            r.setdefault("confirmed_by", "user")
            try:
                intent.add_rule(store.state, r, reg)
                print(f"Added rule {r['id']}.")
            except intent.IntentError as e:
                print(f"Error: {e}", file=sys.stderr)
                errors += 1
        store.save()
        return 1 if errors else 0

    if args.cmd == "accept":
        only = args.only.split(",") if args.only else None
        added, errors = proposer.accept(store.state, args.file, only, reg)
        for cid, rid in added:
            print(f"Added {cid} as {rid}.")
        for e in errors:
            print(f"Error: {e}", file=sys.stderr)
        store.save()
        return 1 if errors else 0

    if args.cmd == "seed" and args.rules and not (args.dismissals or args.keep_file):
        devs, copies, tag = None, None, None
    else:
        copies, devs, tag = collect(mod_root, args)

    if args.cmd == "list":
        states = intent.entry_states(it, copies, devs)
        attributed = {}
        for d in devs:
            a = intent.attribute(d, it, states)
            if a is not None and a.by:
                attributed[a.by] = attributed.get(a.by, 0) + 1
        rows = []
        for r in sorted(it["rules"].values(), key=lambda r: r["id"]):
            if args.system and r["system"] != args.system or args.state:
                continue
            rows.append({"type": "rule", "id": r["id"], "system": r["system"],
                         "disposition": r.get("disposition"), "covers": attributed.get(f"rule:{r['id']}", 0),
                         "reason": r["reason"]})
        for e in sorted(it["entries"].values(), key=lambda e: e["id"]):
            state, why = states.get(e["id"], ("lost", ""))
            if (args.system and e["system"] != args.system) or (args.state and state != args.state):
                continue
            rows.append({"type": "entry", "id": e["id"], "system": e["system"],
                         "disposition": e.get("disposition"), "state": state, "detail": why,
                         "address": e["address"], "reason": e["reason"]})
        if args.json:
            print(json.dumps(rows, indent=2, ensure_ascii=False))
        else:
            if not rows:
                print("The intent store has no rules and no entries.")
            for row in rows:
                tail = (f"covers {row['covers']}" if row["type"] == "rule"
                        else row["state"] + (f" ({row['detail']})" if row["detail"] else ""))
                print(f"{row['type']:5} {row['id']}  [{row['system']}] {row['disposition'] or 'no disposition'}"
                      f", {tail}: {row['reason']}")
        return 0

    if args.cmd == "add":
        hit = [d for d in devs if d.finding and d.finding.startswith(args.finding.lower())]
        ids = {d.finding for d in hit}
        if len(ids) != 1:
            print(f"Error: finding {args.finding} matches {len(ids)} findings; give more characters"
                  if ids else f"Error: no current finding has id {args.finding}", file=sys.stderr)
            return 1
        path = proposer._common_prefix([d.path for d in hit])
        entry = intent.entry_from(hit[0], f"e-{hit[0].finding[:8]}", args.system, args.reason,
                                  _source(args.source), args.disposition, scope=args.scope, today=today)
        entry["address"]["path"] = path
        intent.seal(entry, copies, devs)
        try:
            intent.add_entry(store.state, entry, reg)
        except intent.IntentError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        store.save()
        print(f"Added entry {entry['id']} for {len(hit)} deviation(s) at {entry['address']['identity']}.")
        return 0

    if args.cmd == "confirm":
        e = it["entries"].get(args.id)
        if e is None:
            print(f"Error: no entry has id {args.id}", file=sys.stderr)
            return 1
        intent.seal(e, copies, devs)
        store.save()
        print(f"Confirmed {args.id} as the node reads at {e['seen'] and e['seen'].get('vanilla')}.")
        return 0

    notes = proposer.read_notes(proposer.notes_dir(mod_root))
    if args.cmd == "propose":
        cands = proposer.propose(mod_root, copies, devs, store.state, notes, today, args.system)
    else:
        cands = []
        if args.dismissals:
            cands += proposer.seed_dismissals(copies, devs, store.state, today)
        if args.keep_file:
            found, unmatched = proposer.seed_keep_file(copies, devs, args.keep_file, today)
            cands += found
            for line in unmatched:
                print(f"Note: no deviation matches the keep line {line}", file=sys.stderr)
        if args.rules:
            if devs is None:
                copies, devs, tag = collect(mod_root, args)
            cands += proposer.seed_rules(devs, args.rules, notes, today)
    path = proposer.write(cands, store.dir, store.mod_id, tag, args.out)
    print(f"Wrote {len(cands)} candidate(s) to {path}")
    print("Edit the reasons, systems and dispositions there, then run "
          f"`pdx-audit intent accept {path} [--only c1,c2]`.")
    return 0
