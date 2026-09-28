"""boardkit deploy | apply | plan | audit"""
from __future__ import annotations

import argparse
import json
import sys

from . import __version__


def _print(reports: list[dict], as_json: bool) -> int:
    bad = any(r["problems"] for r in reports)
    if as_json:
        print(json.dumps(reports, indent=1, ensure_ascii=False))
        return 1 if bad else 0
    for r in reports:
        print(f"== {r['project']} on {r['host']}")
        for p in r["published"]:
            note = f"  (kept {', '.join(p['kept'])})" if p["kept"] else ""
            print(f"  published {p['uid']}: {'OK' if not p['problems'] else 'MISMATCH'}{note}")
        for c in r["changed"]:
            print(f"  changed  {c}")
        for e, link in sorted(r["invites"].items()):
            print(f"  invite   {e}: {link}")
        for w in r["warnings"]:
            print(f"  WARNING  {w}")
        for p in r["problems"]:
            print(f"  PROBLEM  {p}")
        if not (r["changed"] or r["problems"]):
            print("  in sync")
    return 1 if bad else 0


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="boardkit")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("deploy", help="ship a repo's declaration to its host and apply it")
    d.add_argument("declaration")
    d.add_argument("--access-only", action="store_true", help="do not republish boards")
    d.add_argument("--reset-vars", action="store_true",
                   help="drop viewers' variable values (keeps preference_vars)")
    d.add_argument("--json", action="store_true")

    a = sub.add_parser("apply", help="(on the host) apply stored bundles")
    g = a.add_mutually_exclusive_group(required=True)
    g.add_argument("--project")
    g.add_argument("--all", action="store_true")
    a.add_argument("--access-only", action="store_true")
    a.add_argument("--reset-vars", action="store_true")
    a.add_argument("--json", action="store_true")

    p = sub.add_parser("plan", help="validate a declaration and show the groups it makes")
    p.add_argument("declaration")

    au = sub.add_parser("audit", help="(on the host) what its Grafana enforces")
    au.add_argument("--host", help="inventory entry (default: this machine)")

    args = ap.parse_args(argv)
    if args.cmd == "deploy":
        from .deploy import deploy
        sys.exit(_print(deploy(args.declaration, access_only=args.access_only,
                               reset_vars=args.reset_vars), args.json))
    if args.cmd == "apply":
        from .deploy import apply_stored
        from .hosts import load_inventory
        inv = load_inventory()
        host = inv.hosts[inv.self_name]
        sys.exit(_print(apply_stored(host, None if args.all else args.project,
                                     access_only=args.access_only,
                                     reset_vars=args.reset_vars), args.json))
    if args.cmd == "plan":
        from .decl import load
        from .policy import static_errors
        decl = load(args.declaration)
        print(f"project {decl.project}: audience {decl.audience}, {decl.sensitivity}")
        for f in decl.folders:
            who = "every account" if f.viewers == "all" else \
                f"team {decl.folder_team(f)!r} = {f.viewers}"
            print(f"  folder {f.title} ({f.uid}): {who}")
            for b in f.boards:
                extra = (f"; + team {decl.board_team(b)!r} = {b.viewers}"
                         f"{' (home)' if b.home else ''}") if b.viewers else ""
                print(f"    board {b.uid}{extra}")
        errs = static_errors(decl)
        for e in errs:
            print(f"  PROBLEM {e}")
        sys.exit(1 if errs else 0)
    if args.cmd == "audit":
        from .audit import audit
        from .hosts import load_inventory
        inv = load_inventory()
        host = inv.hosts[args.host or inv.self_name]
        print(json.dumps(audit(host.grafana()), indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
