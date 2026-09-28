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
  * an org the project names is the project's: its members are exactly the
    viewers placed in it (Admins stay). An ISOLATED board lives in an org of
    its own whose members are its viewers, with the board as the org's home;
    someone who is a viewer only of isolated boards (in any project stored on
    the host) is taken out of the main org, where they could query its
    datasources;
  * an address with no account gets a Viewer invite into the org it belongs
    in, and the link is returned (these hosts have no SMTP). The next apply
    seats the account once it exists, so the host runs
    `boardkit apply --all --access-only` on a timer.

Accounts are org Viewers. Role None cannot read datasources (every panel
shows "Access denied"), and Editor can edit: both are reported.
"""
from __future__ import annotations

from .decl import ALL, Declaration
from .grafana import Grafana, q
from .orgs import MAIN_ORG_ID, org_members

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


class _Run:
    """One apply: the log, the invites, the problems, and caches."""

    def __init__(self, g: Grafana, root_url: str):
        self.g, self.root_url = g, root_url.rstrip("/")
        self.log: list[str] = []
        self.problems: list[str] = []
        self.warnings: list[str] = []
        self.invites: dict[str, str] = {}
        self.seated: set[str] = set()
        self.team_names: dict[int, str] = {}
        self._users: dict[str, dict | None] = {}

    def user(self, email: str) -> dict | None:
        if email not in self._users:
            self._users[email] = self.g.user_by_email(email)
        return self._users[email]

    def invite(self, go: Grafana, email: str, org_label: str) -> None:
        pending = _pending_invites(go)
        if email not in pending:
            go("/api/org/invites", "POST", {"loginOrEmail": email, "name": email,
                                            "role": "Viewer", "sendEmail": False})
            self.log.append(f"invited {email} to {org_label!r}")
            pending = _pending_invites(go)
        self.invites[email] = f"{self.root_url}/invite/{pending[email]['code']}"


def _sync_org(run: _Run, oid: int, label: str, emails: set[str], exact: bool) -> None:
    """Org members := emails (exact for a project-owned org; add-only for the
    main org, which other projects share)."""
    members = org_members(run.g, oid)
    go = run.g.in_org(oid)
    for email in sorted(emails):
        u = run.user(email)
        if u is None:
            run.invite(go, email, label)
        elif email not in members:
            run.g(f"/api/orgs/{oid}/users", "POST", {"loginOrEmail": email, "role": "Viewer"})
            run.log.append(f"added {email} to org {label!r}")
        elif members[email]["role"] not in ("Viewer", "Admin"):
            run.problems.append(f"{email} has role {members[email]['role']} in {label!r} "
                                "(want Viewer: None breaks every panel, Editor can edit)")
    if exact:
        for email, m in members.items():
            if email not in emails and m["role"] != "Admin":
                run.g(f"/api/orgs/{oid}/users/{m['userId']}", "DELETE")
                run.log.append(f"removed {email} from org {label!r}")


def _sync_team(run: _Run, go: Grafana, oid: int, name: str, emails: list[str]) -> int:
    """Team members := emails that have an account in this org."""
    tid = _ensure_team(go, name, run.log)
    run.team_names[tid] = name
    members = org_members(run.g, oid)
    current = {m["email"].lower(): m["userId"] for m in go(f"/api/teams/{tid}/members")}
    for email in emails:
        u = run.user(email)
        if u is None or email not in members:
            continue                        # invited / added to the org; seated next apply
        run.seated.add(email)
        if email not in current:
            go(f"/api/teams/{tid}/members", "POST", {"userId": u["id"]})
            run.log.append(f"added {email} to {name!r}")
    for email, uid in current.items():
        if email not in emails:
            go(f"/api/teams/{tid}/members/{uid}", "DELETE")
            run.log.append(f"removed {email} from {name!r}")
    return tid


def _set(go: Grafana, kind: str, uid: str, grantee: str, perm: str) -> None:
    go(f"/api/access-control/{kind}/{q(uid)}/{grantee}", "POST", {"permission": perm})


def _converge_acl(run: _Run, go: Grafana, kind: str, uid: str, want_teams: dict[int, str],
                  want_builtin: dict[str, str], where: str = "") -> None:
    """Direct (non-inherited) grants on one folder/board := the wanted ones,
    keeping Admin-level grants."""
    acl = go(f"/api/access-control/{kind}/{q(uid)}")
    have_teams, have_builtin = {}, {}
    at = f"{uid}{where}"
    for p in acl:
        if p.get("isInherited"):
            continue
        if p.get("teamId"):
            have_teams[p["teamId"]] = p["permission"]
        elif p.get("builtInRole"):
            have_builtin[p["builtInRole"]] = p["permission"]
        elif p.get("userId") and p["permission"] != "Admin":
            _set(go, kind, uid, f"users/{p['userId']}", "")
            run.log.append(f"removed user {p.get('userLogin')!r} from {at}")
    for tid, perm in want_teams.items():
        if have_teams.get(tid) != perm:
            _set(go, kind, uid, f"teams/{tid}", perm)
            run.log.append(f"granted {run.team_names.get(tid, tid)!r} {perm} on {at}")
    for tid, perm in have_teams.items():
        if tid not in want_teams and perm != "Admin":
            _set(go, kind, uid, f"teams/{tid}", "")
            if tid not in run.team_names:
                run.team_names[tid] = (go(f"/api/teams/{tid}", ok404=True) or {}).get(
                    "name", str(tid))
            run.log.append(f"removed team {run.team_names[tid]!r} from {at}")
    for role, perm in want_builtin.items():
        if have_builtin.get(role) != perm:
            _set(go, kind, uid, f"builtInRoles/{role}", perm)
            run.log.append(f"granted builtin {role} {perm} on {at}")
    for role, perm in have_builtin.items():
        if role not in want_builtin and perm != "Admin" and role != "Admin":
            _set(go, kind, uid, f"builtInRoles/{role}", "")
            run.log.append(f"removed builtin {role} from {at}")


def placement_viewers(decl: Declaration):
    """(org name | None, folder, board, isolated, viewers who may open it there)."""
    for f in decl.folders:
        fv = set(f.viewers) if isinstance(f.viewers, list) else set()
        for b in f.boards:
            in_folder_org = fv | (set() if b.org else set(b.viewers))
            yield f.org, f, b, False, in_folder_org
            if b.org:
                yield b.org, f, b, True, set(b.viewers)


def isolated_only(decls) -> set[str]:
    """Viewers of isolated boards who are not a viewer of anything in the main org."""
    iso, main = set(), set()
    for d in decls:
        for org, _f, _b, isolated, who in placement_viewers(d):
            if isolated:
                iso |= who
            elif org is None:
                main |= who
    return iso - main


def apply_access(g: Grafana, decl: Declaration, root_url: str, org_ids: dict,
                 others=()) -> dict:
    run = _Run(g, root_url)

    # 1. org membership: project-owned orgs exactly; the main org add-only
    want: dict[str | None, set[str]] = {}
    for org, _f, _b, _iso, who in placement_viewers(decl):
        want.setdefault(org, set()).update(who)
    for org, emails in want.items():
        _sync_org(run, org_ids[org], org or "main", emails, exact=org is not None)

    # 2. teams and grants in each folder's org
    for f in decl.folders:
        oid = org_ids[f.org]
        go = g.in_org(oid)
        where = f" in {f.org!r}" if f.org else ""
        fteam = decl.folder_team(f)
        want_teams = {_sync_team(run, go, oid, fteam, f.viewers): VIEW} if fteam else {}
        want_builtin = {"Viewer": VIEW} if f.viewers == ALL else {}
        _converge_acl(run, go, "folders", f.uid, want_teams, want_builtin, where)
        for b in f.boards:
            bteam = decl.board_team(b)
            if not bteam or b.org:          # isolated: its viewers open it in its own org
                _converge_acl(run, go, "dashboards", b.uid, {}, {}, where)
                continue
            tid = _sync_team(run, go, oid, bteam, b.viewers)
            _converge_acl(run, go, "dashboards", b.uid, {tid: VIEW}, {}, where)
            if b.home:
                prefs = go(f"/api/teams/{tid}/preferences")
                if prefs.get("homeDashboardUID") != b.uid:
                    go(f"/api/teams/{tid}/preferences", "PUT", {"homeDashboardUID": b.uid})
                    run.log.append(f"home of {bteam!r} = {b.uid}")

    # 3. isolated boards: the org is the audience, so builtin Viewer = them
    for f, b in decl.boards():
        if not b.org:
            continue
        oid = org_ids[b.org]
        go = g.in_org(oid)
        where = f" in {b.org!r}"
        run.seated |= set(b.viewers) & set(org_members(g, oid))
        _converge_acl(run, go, "folders", f.uid, {}, {"Viewer": VIEW}, where)
        _converge_acl(run, go, "dashboards", b.uid, {}, {}, where)
        if b.home:
            prefs = go("/api/org/preferences")
            if prefs.get("homeDashboardUID") != b.uid:
                go("/api/org/preferences", "PUT", {"homeDashboardUID": b.uid})
                run.log.append(f"home of org {b.org!r} = {b.uid}")

    # 4. out of the main org: viewers who belong only in isolated orgs
    if decl.sensitivity == "sensitive":
        main = org_members(g, MAIN_ORG_ID)
        for email in sorted(isolated_only([decl, *others])):
            m = main.get(email)
            if m and m["role"] != "Admin":
                g(f"/api/orgs/{MAIN_ORG_ID}/users/{m['userId']}", "DELETE")
                run.log.append(f"removed {email} from the main org (their boards are "
                               "isolated; there they could query its datasources)")

    declared = {decl.folder_team(f) for f in decl.folders} | \
               {decl.board_team(b) for _, b in decl.boards() if not b.org}
    for org in {None, *decl.owned_orgs()}:
        go = g.in_org(org_ids[org])
        for t in go(f"/api/teams/search?perpage=1000&query={q(decl.team_prefix())}")["teams"]:
            if t["name"].startswith(decl.team_prefix()) and t["name"] not in declared:
                run.warnings.append(f"team {t['name']!r} is named for this project but no "
                                    "longer declared (delete it by hand if it is stale)")
    return {"changed": run.log, "invites": run.invites, "problems": run.problems,
            "warnings": run.warnings, "seated": sorted(run.seated)}
