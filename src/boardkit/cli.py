"""boardkit deploy | apply | plan | audit"""
from __future__ import annotations

import argparse
import json
import sys

from . import __version__


def _print(reports: list[dict], as_json: bool, redact: bool = False) -> int:
    bad = any(r["problems"] for r in reports)
    if redact:                  # a log on disk must never hold a password
        for r in reports:
            r["new_accounts"] = {e: "<redacted>" for e in r.get("new_accounts", {})}
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
        for e, pw in sorted(r.get("new_accounts", {}).items()):
            print(f"  ACCOUNT  {e}  password {pw}   (shown once: hand it over, they change it)")
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
    a.add_argument("--redact", action="store_true", help="never print new passwords (timers)")

    ad = sub.add_parser("adopt", help="(on the host) vouch that an existing account is this "
                                      "declared person, and pin it")
    ad.add_argument("emails", nargs="*")
    ad.add_argument("--declared", action="store_true",
                    help="every declared viewer of the stored projects whose account has "
                         "login == email == the declared address")
    un = sub.add_parser("unpin", help="(on the host) forget an address's pinned account")
    un.add_argument("emails", nargs="+")

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
                                     reset_vars=args.reset_vars), args.json, args.redact))
    if args.cmd in ("adopt", "unpin"):
        from .grafana import q
        from .hosts import load_inventory
        from .identities import Identities
        inv = load_inventory()
        host = inv.hosts[inv.self_name]
        ids = Identities(host.store_path / "identities.json")
        if args.cmd == "unpin":
            for e in args.emails:
                print(f"{e}: {'unpinned' if ids.unpin(e.lower()) else 'was not pinned'}")
            return
        emails = [e.lower() for e in args.emails]
        if args.declared:
            from .access import placement_viewers
            from .deploy import load_bundle
            root = host.store_path / "projects"
            for d in sorted(p for p in root.iterdir() if (p / "declaration.json").is_file()):
                for _o, _f, _b, _i, who in placement_viewers(load_bundle(d)):
                    emails += sorted(who)
        g = host.grafana()
        bad = 0
        for e in dict.fromkeys(emails):
            if ids.get(e) is not None:
                continue
            hits = [x for x in g(f"/api/users/search?perpage=100&query={q(e)}")["users"]
                    if (x.get("login") or "").lower() == e or (x.get("email") or "").lower() == e]
            exact = [x for x in hits if (x.get("login") or "").lower() == e
                     and (x.get("email") or "").lower() == e]
            if len(hits) == 1 and exact:
                ids.pin(e, exact[0]["id"])
                print(f"{e}: pinned account #{exact[0]['id']}")
            elif not hits:
                print(f"{e}: no account (the next apply creates one)")
            else:
                bad += 1
                print(f"{e}: NOT pinned, ambiguous or mismatched: "
                      + "; ".join(f"#{x['id']} login {x.get('login')!r} email {x.get('email')!r}"
                                  for x in hits))
        sys.exit(1 if bad else 0)
    if args.cmd == "plan":
        from .decl import load
        from .policy import static_errors
        decl = load(args.declaration)
        print(f"project {decl.project}: audience {decl.audience}, {decl.sensitivity}")
        for f in decl.folders:
            who = "every account" if f.viewers == "all" else \
                f"team {decl.folder_team(f)!r} = {f.viewers}"
            print(f"  folder {f.title} ({f.uid}) in org {f.org or 'main'!r}: {who}")
            for b in f.boards:
                home = " (home)" if b.home else ""
                if b.org:
                    extra = (f"; isolated in org {b.org!r} = {b.viewers}{home}, "
                             f"datasources {decl.datasource_map(f, b, True)}")
                elif b.viewers:
                    extra = f"; + team {decl.board_team(b)!r} = {b.viewers}{home}"
                else:
                    extra = ""
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
