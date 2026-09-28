"""Make the host's access match a declaration, and say what changed.

Model (Grafana OSS 13):
  * every group of viewers is a TEAM whose members are exactly the declared
    emails: someone taken out of the declaration is taken out of the team;
  * a folder's viewers hold View on the folder; "all" is the builtin Viewer
    role; a board's own viewers hold View on that board only, and the board can
    be their team's home page;
  * a project's folders and boards carry exactly the grants it declares. Other
    team, user or builtin grants on them are removed; Admin-level grants stay,
    since admins see everything anyway and the operator's own account holds one;
  * an address with no account gets a Viewer invite, and the link is returned
    (these hosts have no SMTP). The next apply seats the account once it
    exists, so the host runs `boardkit apply --all --access-only` on a timer.

Accounts are org Viewers. Role None cannot read datasources (every panel
shows "Access denied"), and Editor can edit: both are reported.
"""
from __future__ import annotations

from .decl import ALL, Declaration
from .grafana import Grafana, q

VIEW = "View"


def _ensure_team(g: Grafana, name: str, log: list[str]) -> int:
    t = g.team_by_name(name)
    if t:
        return t["id"]
    tid = g("/api/teams", "POST", {"name": name})["teamId"]
    log.append(f"created team {name!r}")
    return tid


def _pending_invites(g: Grafana) -> dict[str, dict]:
    return {i["email"].lower(): i for i in g("/api/org/invites")}


def _sync_members(g: Grafana, tid: int, team: str, emails: list[str], root_url: str,
                  org_roles: dict[int, str], log, invites, problems) -> set[str]:
    """Team members := emails. Returns the emails that have an account."""
    current = {m["email"].lower(): m["userId"] for m in g(f"/api/teams/{tid}/members")}
    have = set()
    pending = None
    for email in emails:
        u = g.user_by_email(email)
        if u is None:
            if pending is None:
                pending = _pending_invites(g)
            if email not in pending:
                g("/api/org/invites", "POST", {"loginOrEmail": email, "name": email,
                                               "role": "Viewer", "sendEmail": False})
                log.append(f"invited {email}")
                pending = _pending_invites(g)
            invites[email] = f"{root_url.rstrip('/')}/invite/{pending[email]['code']}"
            continue
        have.add(email)
        if email not in current:
            g(f"/api/teams/{tid}/members", "POST", {"userId": u["id"]})
            log.append(f"added {email} to {team!r}")
        role = org_roles.get(u["id"])
        if role not in ("Viewer", "Admin"):          # Admin: an operator, sees all anyway
            problems.append(f"{email} has org role {role} (want Viewer: None breaks "
                            "every panel, Editor can edit)")
    for email, uid in current.items():
        if email not in emails:
            g(f"/api/teams/{tid}/members/{uid}", "DELETE")
            log.append(f"removed {email} from {team!r}")
    return have


def _set(g: Grafana, kind: str, uid: str, grantee: str, perm: str) -> None:
    g(f"/api/access-control/{kind}/{q(uid)}/{grantee}", "POST", {"permission": perm})


def _converge_acl(g: Grafana, kind: str, uid: str, want_teams: dict[int, str],
                  want_builtin: dict[str, str], names: dict[int, str], log: list[str]) -> None:
    """Direct (non-inherited) grants on one folder/board := the wanted ones,
    keeping Admin-level grants."""
    acl = g(f"/api/access-control/{kind}/{q(uid)}")
    have_teams, have_builtin = {}, {}
    for p in acl:
        if p.get("isInherited"):
            continue
        if p.get("teamId"):
            have_teams[p["teamId"]] = p["permission"]
        elif p.get("builtInRole"):
            have_builtin[p["builtInRole"]] = p["permission"]
        elif p.get("userId") and p["permission"] != "Admin":
            _set(g, kind, uid, f"users/{p['userId']}", "")
            log.append(f"removed user {p.get('userLogin')!r} from {uid}")
    for tid, perm in want_teams.items():
        if have_teams.get(tid) != perm:
            _set(g, kind, uid, f"teams/{tid}", perm)
            log.append(f"granted {names.get(tid, tid)!r} {perm} on {uid}")
    for tid, perm in have_teams.items():
        if tid not in want_teams and perm != "Admin":
            _set(g, kind, uid, f"teams/{tid}", "")
            if tid not in names:
                names[tid] = (g(f"/api/teams/{tid}", ok404=True) or {}).get("name", str(tid))
            log.append(f"removed team {names[tid]!r} from {uid}")
    for role, perm in want_builtin.items():
        if have_builtin.get(role) != perm:
            _set(g, kind, uid, f"builtInRoles/{role}", perm)
            log.append(f"granted builtin {role} {perm} on {uid}")
    for role, perm in have_builtin.items():
        if role not in want_builtin and perm != "Admin" and role != "Admin":
            _set(g, kind, uid, f"builtInRoles/{role}", "")
            log.append(f"removed builtin {role} from {uid}")


def apply_access(g: Grafana, decl: Declaration, root_url: str) -> dict:
    log: list[str] = []
    problems: list[str] = []
    warnings: list[str] = []
    invites: dict[str, str] = {}
    org_roles = {u["userId"]: u["role"] for u in g("/api/org/users")}
    names: dict[int, str] = {}
    seated: set[str] = set()

    def team_for(name, emails):
        tid = _ensure_team(g, name, log)
        names[tid] = name
        seated.update(_sync_members(g, tid, name, emails, root_url, org_roles,
                                    log, invites, problems))
        return tid

    for f in decl.folders:
        fteam = decl.folder_team(f)
        want_teams = {team_for(fteam, f.viewers): VIEW} if fteam else {}
        want_builtin = {"Viewer": VIEW} if f.viewers == ALL else {}
        _converge_acl(g, "folders", f.uid, want_teams, want_builtin, names, log)
        for b in f.boards:
            bteam = decl.board_team(b)
            if not bteam:
                _converge_acl(g, "dashboards", b.uid, {}, {}, names, log)
                continue
            tid = team_for(bteam, b.viewers)
            _converge_acl(g, "dashboards", b.uid, {tid: VIEW}, {}, names, log)
            if b.home:
                prefs = g(f"/api/teams/{tid}/preferences")
                if prefs.get("homeDashboardUID") != b.uid:
                    g(f"/api/teams/{tid}/preferences", "PUT", {"homeDashboardUID": b.uid})
                    log.append(f"home of {bteam!r} = {b.uid}")

    declared = {decl.folder_team(f) for f in decl.folders} | \
               {decl.board_team(b) for _, b in decl.boards()}
    for t in g(f"/api/teams/search?perpage=1000&query={q(decl.team_prefix())}")["teams"]:
        if t["name"].startswith(decl.team_prefix()) and t["name"] not in declared:
            warnings.append(f"team {t['name']!r} is named for this project but no longer "
                            "declared (delete it by hand if it is stale)")
    return {"changed": log, "invites": invites, "problems": problems,
            "warnings": warnings, "seated": sorted(seated)}
