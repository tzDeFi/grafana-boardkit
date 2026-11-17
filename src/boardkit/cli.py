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
            where = f" in {p['org']!r}" if p.get("org") else ""
            print(f"  published {p['uid']}{where}: "
                  f"{'OK' if not p['problems'] else 'MISMATCH'}{note}")
        for c in r["changed"]:
            print(f"  changed  {c}")
        for w in r["warnings"]:
            print(f"  WARNING  {w}")
        for p in r["problems"]:
            print(f"  PROBLEM  {p}")
        if not (r["changed"] or r["problems"]):
            print("  in sync")
    return 1 if bad else 0


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(
        prog="boardkit",
        description="Publish Grafana boards. Who may open them is set in Grafana, "
                    "never here.")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("deploy", help="ship a repo's declaration to its host and publish it")
    d.add_argument("declaration")
    d.add_argument("--reset-vars", action="store_true",
                   help="drop viewers' variable values (keeps preference_vars)")
    d.add_argument("--json", action="store_true")

    a = sub.add_parser("apply", help="(on the host) publish one stored project again")
    a.add_argument("--project", required=True)
    a.add_argument("--reset-vars", action="store_true")
    a.add_argument("--json", action="store_true")

    p = sub.add_parser("plan", help="validate a declaration and show what it publishes")
    p.add_argument("declaration")

    au = sub.add_parser("audit", help="(on the host) what its Grafana enforces: read-only")
    au.add_argument("--host", help="inventory entry (default: this machine)")

    args = ap.parse_args(argv)
    if args.cmd == "deploy":
        from .deploy import deploy
        sys.exit(_print(deploy(args.declaration, reset_vars=args.reset_vars), args.json))
    if args.cmd == "apply":
        from .deploy import apply_stored
        from .hosts import load_inventory
        inv = load_inventory()
        host = inv.hosts[inv.self_name]
        sys.exit(_print(apply_stored(host, args.project, reset_vars=args.reset_vars),
                        args.json))
    if args.cmd == "plan":
        from .decl import load
        decl = load(args.declaration)
        print(f"project {decl.project}: audience {decl.audience}, {decl.sensitivity}")
        for f in decl.folders:
            print(f"  folder {f.title} ({f.uid}) in org {f.org or 'main'!r}")
            for b in f.boards:
                extra = (f"; also in org {b.org!r}, datasources "
                         f"{decl.datasource_map(f, b, True)}") if b.org else ""
                print(f"    board {b.uid}{extra}")
        print("  access: not declared here; set it in Grafana (Administration, Users and access)")
        return
    if args.cmd == "audit":
        from .audit import audit
        from .hosts import load_inventory
        inv = load_inventory()
        host = inv.hosts[args.host or inv.self_name]
        print(json.dumps(audit(host.grafana()), indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
