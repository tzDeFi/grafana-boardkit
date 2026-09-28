"""Make the host's access match a declaration, and say what changed.

Model (Grafana OSS 13):
  * every group of viewers is a TEAM whose members are exactly the declared
    people: someone taken out of the declaration is taken out of the team;
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
    datasources.

People are ACCOUNTS PINNED BY ID (boardkit.identities), never names: a
declared address with no account gets one created on the spot (login = email =
the address) and a one-time password in the report for the operator to hand
over (these hosts have no SMTP); an address held by an account boardkit did not
create is not seated until `boardkit adopt`; a pinned account whose login or
email has changed is not seated. Every membership is compared and changed by
user id.

Accounts are org Viewers. Role None cannot read datasources (every panel
shows "Access denied"), and Editor can edit: both are reported.
"""
from __future__ import annotations

import secrets

from .decl import ALL, Declaration
from .grafana import Grafana, GrafanaError, q
from .identities import Identities
from .orgs import MAIN_ORG_ID

VIEW = "View"


def _ensure_team(g: Grafana, name: str, log: list[str]) -> int:
    t = g.team_by_name(name)
    if t:
        return t["id"]
    tid = g("/api/teams", "POST", {"name": name})["teamId"]
    log.append(f"created team {name!r}")
    return tid


def _members(g: Grafana, oid: int) -> dict[int, dict]:
    return {m["userId"]: m for m in g(f"/api/orgs/{oid}/users")}


def _matches(u: dict, email: str) -> bool:
    return (u.get("login") or "").lower() == email and (u.get("email") or "").lower() == email


class _Run:
    """One apply: the log, the new accounts, the problems, and caches."""

    def __init__(self, g: Grafana, root_url: str, ids: Identities):
        self.g, self.root_url, self.ids = g, root_url.rstrip("/"), ids
        self.log: list[str] = []
        self.problems: list[str] = []
        self.warnings: list[str] = []
        self.new_accounts: dict[str, str] = {}
        self.seated: set[str] = set()
        self.team_names: dict[int, str] = {}
        self._acct: dict[str, dict | None] = {}

    def account(self, email: str, org_for_new: int | None = None) -> dict | None:
        """The account that IS this declared address, or None (with the reason
        in problems). Creates and pins it if nobody holds the address."""
        if email in self._acct:
            return self._acct[email]
        uid = self.ids.get(email)
        u = None
        if uid is not None:
            u = self.g(f"/api/users/{uid}", ok404=True)
            if u is None:
                self.problems.append(f"{email}: its pinned account #{uid} was deleted; not "
                                     f"seated (`boardkit unpin {email}` to create a new one)")
            elif u.get("isDisabled"):
                self.warnings.append(f"{email}: account #{uid} is disabled; not seated")
                u = None
            elif not _matches(u, email):
                self.problems.append(
                    f"{email}: pinned account #{uid} is now login {u.get('login')!r}, email "
                    f"{u.get('email')!r}; not seated (renamed? restore it, or unpin)")
                u = None
            else:
                u = {"id": uid, "login": u["login"], "email": u["email"]}
        else:
            hits = [x for x in self.g(f"/api/users/search?perpage=100&query={q(email)}")["users"]
                    if (x.get("login") or "").lower() == email
                    or (x.get("email") or "").lower() == email]
            if not hits:
                pw = secrets.token_urlsafe(15)
                body = {"name": email, "email": email, "login": email, "password": pw}
                if org_for_new:
                    body["OrgId"] = org_for_new
                new_id = self.g("/api/admin/users", "POST", body)["id"]
                self.ids.pin(email, new_id)
                self.new_accounts[email] = pw
                self.log.append(f"created account {email} (#{new_id})")
                u = {"id": new_id, "login": email, "email": email}
            else:
                held = "; ".join(f"#{x['id']} login {x.get('login')!r} email {x.get('email')!r}"
                                 for x in hits)
                self.problems.append(
                    f"{email}: already held by an account boardkit did not create ({held}); "
                    f"not seated. If it is theirs: `boardkit adopt {email}` on the host")
        self._acct[email] = u
        return u


def _sync_org(run: _Run, oid: int, label: str, emails: set[str], exact: bool) -> set[int]:
    """Org members := these people, by id (exact for a project-owned org;
    add-only for the main org, which other projects share)."""
    members = _members(run.g, oid)
    want: set[int] = set()
    for email in sorted(emails):
        u = run.account(email, oid)
        if u is None:
            continue
        want.add(u["id"])
        if u["id"] not in members:
            try:
                r = run.g(f"/api/orgs/{oid}/users", "POST",
                          {"loginOrEmail": u["login"], "role": "Viewer"})
            except GrafanaError as e:
                if e.status != 409:             # "already member": a concurrent apply
                    raise
                r = {"userId": u["id"]}
            if r.get("userId") not in (None, u["id"]):
                run.problems.append(f"adding {email} to {label!r} seated account "
                                    f"#{r.get('userId')}, not #{u['id']}: check the org")
            run.log.append(f"added {email} to org {label!r}")
        elif members[u["id"]]["role"] not in ("Viewer", "Admin"):
            run.problems.append(f"{email} has role {members[u['id']]['role']} in {label!r} "
                                "(want Viewer: None breaks every panel, Editor can edit)")
    if exact:
        for uid, m in members.items():
            if uid not in want and m["role"] != "Admin":
                run.g(f"/api/orgs/{oid}/users/{uid}", "DELETE")
                run.log.append(f"removed {m.get('login')} (#{uid}) from org {label!r}")
    return want


def _sync_team(run: _Run, go: Grafana, oid: int, name: str, emails: list[str]) -> int:
    """Team members := these people, by id, when they are in this org."""
    tid = _ensure_team(go, name, run.log)
    run.team_names[tid] = name
    in_org = set(_members(run.g, oid))
    current = {m["userId"]: m for m in go(f"/api/teams/{tid}/members")}
    want: set[int] = set()
    for email in emails:
        u = run.account(email, oid)
        if u is None or u["id"] not in in_org:
            continue
        want.add(u["id"])
        run.seated.add(email)
        if u["id"] not in current:
            go(f"/api/teams/{tid}/members", "POST", {"userId": u["id"]})
            run.log.append(f"added {email} to {name!r}")
    for uid, m in current.items():
        if uid not in want:
            go(f"/api/teams/{tid}/members/{uid}", "DELETE")
            run.log.append(f"removed {m.get('login') or m.get('email')} (#{uid}) from {name!r}")
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
                 others=(), identities: Identities | None = None) -> dict:
    run = _Run(g, root_url, identities or Identities(None))

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
        in_org = set(_members(g, oid))
        run.seated |= {e for e in b.viewers if (u := run.account(e)) and u["id"] in in_org}
        _converge_acl(run, go, "folders", f.uid, {}, {"Viewer": VIEW}, where)
        _converge_acl(run, go, "dashboards", b.uid, {}, {}, where)
        if b.home:
            prefs = go("/api/org/preferences")
            if prefs.get("homeDashboardUID") != b.uid:
                go("/api/org/preferences", "PUT", {"homeDashboardUID": b.uid})
                run.log.append(f"home of org {b.org!r} = {b.uid}")

    # 4. out of the main org: viewers who belong only in isolated orgs
    if decl.sensitivity == "sensitive":
        main = _members(g, MAIN_ORG_ID)
        for email in sorted(isolated_only([decl, *others])):
            u = run.account(email)
            m = main.get(u["id"]) if u else None
            if m and m["role"] != "Admin":
                g(f"/api/orgs/{MAIN_ORG_ID}/users/{u['id']}", "DELETE")
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
    return {"changed": run.log, "new_accounts": run.new_accounts, "problems": run.problems,
            "warnings": run.warnings, "seated": sorted(run.seated)}
