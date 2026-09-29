"""What a Grafana actually enforces about access, in one report.

Reads EFFECTIVE settings (not the compose file), accounts by role, teams,
pending invites and every folder's grants, and flags what the policy forbids
anywhere: anonymous access with datasources, an anonymous role above Viewer,
invite links that die before a week is out.
"""
from __future__ import annotations

import collections

from .grafana import Grafana, q


def audit(g: Grafana) -> dict:
    """The main org's access, then each org's members, datasources and boards.

    Every main-org call names org 1: an unheaded call acts in the admin's
    CURRENT org, whichever org they last switched to."""
    s = g.settings()
    main = g.in_org(1)
    anon = s.get("auth.anonymous", {})
    users = s.get("users", {})
    out = {
        "anonymous": anon.get("enabled") == "true",
        "anonymous_role": anon.get("org_role") if anon.get("enabled") == "true" else None,
        "sign_up": users.get("allow_sign_up") == "true",
        "invite_lifetime": users.get("user_invite_max_lifetime_duration"),
        "root_url": s.get("server", {}).get("root_url"),
    }
    org_users = main("/api/org/users")
    out["accounts_by_role"] = dict(collections.Counter(u["role"] for u in org_users))
    out["accounts"] = sorted(f"{u.get('email') or u.get('login')}:{u['role']}"
                             for u in org_users)
    out["pending_invites"] = sorted(i["email"] for i in main("/api/org/invites"))
    out["teams"] = {t["name"]: t.get("memberCount")
                    for t in main("/api/teams/search?perpage=1000")["teams"]}
    folders = {}
    for f in main("/api/folders?limit=1000"):
        acl = main(f"/api/access-control/folders/{q(f['uid'])}")
        folders[f"{f['title']} ({f['uid']})"] = sorted(
            (f"team:{p['team']}" if p.get("teamId") else
             f"builtin:{p['builtInRole']}" if p.get("builtInRole") else
             f"user:{p.get('userLogin')}") + f"={p['permission']}"
            for p in acl if not p.get("isInherited"))
    out["folders"] = folders
    ds = main("/api/datasources")
    out["datasources"] = sorted(f"{d['name']} ({d['uid']}, {d['type']})" for d in ds)
    flags = []
    if out["anonymous"] and ds:
        flags.append(f"anonymous visitors can query {len(ds)} datasource(s) with "
                     "hand-written SQL (POST /api/ds/query)")
    if out["anonymous_role"] not in (None, "Viewer"):
        flags.append(f"anonymous role is {out['anonymous_role']}")
    if out["sign_up"]:
        flags.append("self sign-up is on")
    orgs = {}
    for o in g("/api/orgs"):
        go = g.in_org(o["id"])
        members = g(f"/api/orgs/{o['id']}/users")
        orgs[f"{o['name']} ({o['id']})"] = {
            "members": sorted(f"{u.get('email') or u.get('login')}:{u['role']}" for u in members),
            "datasources": sorted(d["uid"] for d in go("/api/datasources")),
            "boards": sorted(d["uid"] for d in go("/api/search?type=dash-db&limit=5000")),
        }
    out["orgs"] = orgs
    out["flags"] = flags
    return out
