"""The whole journey on a real Grafana OSS 13.0.2 (docker), as a repo lives it.

  a project declares boards (no people) -> deploy: folder + boards (one with
  real tabs), datasources rewired, NO account, team or permission created ->
  the operator sets access in Grafana's own admin pages (users, a team, folder
  and board grants, a home page, an invite) -> deploying again, and the host's
  re-apply, leave every bit of it as the operator left it -> what the operator
  removes stays removed.

And the converter is pinned to Grafana's own classic -> v2 conversion, live.
"""
from __future__ import annotations

import base64
import json
import urllib.request

import pytest

from boardkit import publish
from boardkit.deploy import apply_stored, deploy
from boardkit.grafana import AccessIsGrafanas, Grafana, GrafanaError
from boardkit.hosts import load_inventory
from boardkit.v2 import to_v2

from .boards import QUICK_RANGES, plain, tabbed

pytestmark = pytest.mark.e2e

PASSWORD = "Correct-horse-9"


def _admin(c):
    return Grafana(c["url"], user="admin", password=c["password"])


def _as(c, email):
    return Grafana(c["url"], user=email, password=PASSWORD)


def _status(g: Grafana, path: str) -> int:
    try:
        g(path)
        return 200
    except GrafanaError as e:
        return e.status


def _ui(c, path: str, method: str = "GET", body=None, org: int | None = None):
    """What the operator's browser does in Grafana's own admin pages. boardkit's
    client refuses access writes by design, so the test speaks plain HTTP."""
    headers = {"Content-Type": "application/json", "Authorization": "Basic " + base64.b64encode(
        f"admin:{c['password']}".encode()).decode()}
    if org:
        headers["X-Grafana-Org-Id"] = str(org)
    req = urllib.request.Request(c["url"] + path, method=method, headers=headers,
                                 data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}


def _access_state(c) -> dict:
    """Everything that decides who sees what in the main org, as Grafana holds it."""
    def acl(kind, uid):
        return sorted(f"{p.get('userLogin') or p.get('team') or p.get('builtInRole')}"
                      f"={p['permission']}" for p in _ui(c, f"/api/access-control/{kind}/{uid}")
                      if not p.get("isInherited"))
    teams = _ui(c, "/api/teams/search?perpage=1000")["teams"]
    return {
        "users": sorted(f"{u['login']}:{u['role']}" for u in _ui(c, "/api/orgs/1/users")),
        "teams": {t["name"]: sorted(m["login"] for m in _ui(c, f"/api/teams/{t['id']}/members"))
                  for t in teams},
        "team_home": {t["name"]: _ui(c, f"/api/teams/{t['id']}/preferences").get(
            "homeDashboardUID") for t in teams},
        "folder": acl("folders", "bk-alpha"),
        "board": acl("dashboards", "bk-b"),
        "invites": sorted(i["email"] for i in _ui(c, "/api/org/invites")),
    }


@pytest.fixture(scope="module")
def datasource(grafana_container):
    g = _admin(grafana_container)
    if g("/api/datasources/uid/pg-main", ok404=True) is None:
        g("/api/datasources", "POST", {
            "uid": "pg-main", "name": "main", "type": "grafana-postgresql-datasource",
            "access": "proxy", "url": "db.invalid:5432", "user": "ro",
            "jsonData": {"database": "none", "sslmode": "disable"}})
    return "pg-main"


def _write(tmp_path, decl: dict, boards: dict[str, dict]):
    for name, b in boards.items():
        (tmp_path / name).write_text(json.dumps(b))
    p = tmp_path / "boards.json"
    p.write_text(json.dumps(decl))
    return p


ALPHA = {
    "project": "alpha", "audience": "internal", "sensitivity": "sensitive",
    "datasources": {"${DS_MAIN}": "main", "maindb": "main"},
    "folders": [{
        "uid": "bk-alpha", "title": "Alpha", "tabs": True,
        "boards": [{"file": "a.json", "preference_vars": ["loss"]},
                   {"file": "b.json", "tabs": False}]}]}


def test_journey_access_is_grafanas(tmp_path, tmp_inventory, grafana_container, datasource):
    c = grafana_container
    inv = load_inventory(tmp_inventory)
    admin = _admin(c)
    decl = _write(tmp_path, ALPHA, {"a.json": tabbed("bk-a"), "b.json": plain("bk-b")})
    people_before = sorted(f"{u['login']}:{u['role']}" for u in _ui(c, "/api/orgs/1/users"))

    # 1. deploy publishes the boards and creates no one: no account, no team,
    #    no grant, no invite
    [r] = deploy(decl, inventory=inv)
    assert r["problems"] == [], r
    assert {p["uid"] for p in r["published"]} == {"bk-a", "bk-b"}
    assert "new_accounts" not in r
    ns = admin.namespace()
    a = admin(f"/apis/dashboard.grafana.app/v2/namespaces/{ns}/dashboards/bk-a")
    assert a["spec"]["layout"]["kind"] == "TabsLayout"
    assert [t["spec"]["title"] for t in a["spec"]["layout"]["spec"]["tabs"]] == \
        ["Performance", "Risk"]
    b = admin("/api/dashboards/uid/bk-b")["dashboard"]
    assert b["panels"][0]["datasource"] == {"type": "grafana-postgresql-datasource",
                                            "uid": "pg-main"}, "rewired to the host's uid"
    st = _access_state(c)
    assert st["users"] == people_before and not [t for t in st["teams"] if "alpha" in t.lower()]
    assert st["invites"] == []

    # 2. the operator sets access in Grafana's own pages
    ids = {e: _ui(c, "/api/admin/users", "POST", {
        "name": e, "email": f"{e}@example.com", "login": f"{e}@example.com",
        "password": PASSWORD})["id"] for e in ("alice", "bob", "carol")}
    team = _ui(c, "/api/teams", "POST", {"name": "Alpha readers"})["teamId"]
    _ui(c, f"/api/teams/{team}/members", "POST", {"userId": ids["alice"]})
    _ui(c, "/api/access-control/folders/bk-alpha/builtInRoles/Viewer", "POST",
        {"permission": ""})                      # the folder is for its team only
    _ui(c, "/api/access-control/folders/bk-alpha/builtInRoles/Editor", "POST",
        {"permission": ""})
    _ui(c, f"/api/access-control/folders/bk-alpha/teams/{team}", "POST", {"permission": "View"})
    _ui(c, f"/api/access-control/dashboards/bk-b/users/{ids['bob']}", "POST",
        {"permission": "View"})                  # bob: one board, by name
    _ui(c, f"/api/teams/{team}/preferences", "PUT", {"homeDashboardUID": "bk-a"})
    _ui(c, "/api/org/invites", "POST", {"loginOrEmail": "newcomer@example.com", "name": "N",
                                        "role": "Viewer", "sendEmail": False})
    state = _access_state(c)
    alice, bob, carol = (_as(c, f"{n}@example.com") for n in ("alice", "bob", "carol"))

    def sees():
        return {n: (_status(g, "/api/dashboards/uid/bk-a"), _status(g, "/api/dashboards/uid/bk-b"))
                for n, g in (("alice", alice), ("bob", bob), ("carol", carol))}

    want = {"alice": (200, 200), "bob": (403, 200), "carol": (403, 403)}
    assert sees() == want, "the operator's grants work"
    assert alice("/api/dashboards/home").get("redirectUri", "").startswith("/d/bk-a/")

    # 3. deploying again, and the host's re-apply, change none of it
    for _ in range(2):
        [again] = deploy(decl, inventory=inv)
        assert again["problems"] == [] and again["changed"] == [], again
    [stored] = apply_stored(inv.hosts["local"], "alpha")
    assert stored["problems"] == [] and stored["changed"] == [], stored
    assert _access_state(c) == state, "access is exactly as the operator left it"
    assert sees() == want

    # 4. what the operator removes stays removed
    _ui(c, f"/api/teams/{team}/members/{ids['alice']}", "DELETE")
    _ui(c, f"/api/access-control/dashboards/bk-b/users/{ids['bob']}", "POST", {"permission": ""})
    _ui(c, f"/api/orgs/1/users/{ids['carol']}", "DELETE")
    code = next(i["code"] for i in _ui(c, "/api/org/invites")
                if i["email"] == "newcomer@example.com")
    _ui(c, f"/api/org/invites/{code}/revoke", "PATCH")
    state2 = _access_state(c)
    assert state2 != state
    [after] = deploy(decl, inventory=inv)
    assert after["problems"] == [] and after["changed"] == []
    assert _access_state(c) == state2, "nothing was put back"

    # 5. the old behaviour is structurally gone: the client refuses
    with pytest.raises(AccessIsGrafanas):
        admin("/api/teams", "POST", {"name": "boardkit team"})


def _same(a, b, path=""):
    """Deep equality where 1 == 1.0 (Grafana re-serialises floats)."""
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) \
            and not isinstance(a, bool) and not isinstance(b, bool):
        assert a == b, path
        return
    assert type(a) is type(b), f"{path}: {a!r} vs {b!r}"
    if isinstance(a, dict):
        assert set(a) == set(b), f"{path}: keys {set(a) ^ set(b)}"
        for k in a:
            _same(a[k], b[k], f"{path}.{k}")
    elif isinstance(a, list):
        assert len(a) == len(b), f"{path}: len {len(a)} vs {len(b)}"
        for i, (x, y) in enumerate(zip(a, b, strict=True)):
            _same(x, y, f"{path}[{i}]")
    else:
        assert a == b, f"{path}: {a!r} vs {b!r}"


@pytest.mark.parametrize("make, picker, uid", [
    (tabbed, None, "conv-tabbed"), (plain, None, "conv-plain"),
    (tabbed, {"quick_ranges": QUICK_RANGES}, "conv-tabbed-qr"),
    (plain, {"quick_ranges": QUICK_RANGES}, "conv-plain-qr"),
    (plain, {"quick_ranges": []}, "conv-plain-qr-empty")],
    ids=["rows", "no-rows", "rows+quick-ranges", "no-rows+quick-ranges",
         "no-rows+empty-quick-ranges"])
def test_converter_matches_grafanas_own_conversion(grafana_container, datasource, make, picker,
                                                   uid):
    """Store the classic board through the legacy API, read Grafana's v2
    rendering of it, and require to_v2 to produce the same spec (with rows
    swapped for tabs, the one intended difference). With quick ranges: Grafana
    carries them as timeSettings.quickRanges, in order, and an empty list as none."""
    admin = _admin(grafana_container)
    board = make(uid=uid, ds_ref="pg-main")
    if picker:
        board["timepicker"] = picker
    for p in board["panels"]:
        for o in [p, *p.get("targets", [])]:
            if "datasource" in o:
                o["datasource"] = {"type": "grafana-postgresql-datasource", "uid": "pg-main"}
    admin("/api/dashboards/db", "POST", {"dashboard": {**board, "id": None}, "overwrite": True})
    ns = admin.namespace()
    got = admin(f"/apis/dashboard.grafana.app/v2/namespaces/{ns}/dashboards/{board['uid']}")
    spec = got["spec"]
    if spec["layout"]["kind"] == "RowsLayout":
        spec["layout"] = {"kind": "TabsLayout", "spec": {"tabs": [
            {"kind": "TabsLayoutTab", "spec": {"title": r["spec"]["title"],
                                               "layout": r["spec"]["layout"]}}
            for r in spec["layout"]["spec"]["rows"]]}}
    _same(to_v2(board)["spec"], spec)
    assert spec["timeSettings"].get("quickRanges") == ((picker or {}).get("quick_ranges") or None)


QUICK = {"project": "qr", "audience": "internal", "sensitivity": "sensitive",
         "datasources": {"${DS_MAIN}": "main", "maindb": "main"},
         "folders": [{"uid": "bk-qr", "title": "Quick ranges", "tabs": True, "boards": [
             {"file": "t.json"}, {"file": "c.json", "tabs": False}]}]}


def test_deploy_carries_quick_ranges_and_reports_a_dropped_one(
        tmp_path, tmp_inventory, grafana_container, datasource, monkeypatch):
    """0.3.1 dropped `timepicker.quick_ranges` on the way to v2 and the deploy
    read back clean. Now the quick ranges, the range and the timezone of a board
    come back from Grafana as declared, on both publish paths; and a converter
    that loses one is a problem in the deploy's report."""
    admin, inv = _admin(grafana_container), load_inventory(tmp_inventory)
    tabbed_board, classic_board = tabbed("bk-qr-t"), plain("bk-qr-c", "Classic", "${DS_MAIN}")
    tabbed_board["timezone"], classic_board["timezone"] = "Europe/Paris", "utc"
    classic_board["time"] = {"from": "now-90d", "to": "now-1d"}
    for b in (tabbed_board, classic_board):
        b["timepicker"] = {"quick_ranges": QUICK_RANGES}
    decl = _write(tmp_path, QUICK, {"t.json": tabbed_board, "c.json": classic_board})

    [r] = deploy(decl, inventory=inv)
    assert r["problems"] == [], r["problems"]
    ns = admin.namespace()
    v2 = admin(f"/apis/dashboard.grafana.app/v2/namespaces/{ns}/dashboards/bk-qr-t")
    assert v2["spec"]["layout"]["kind"] == "TabsLayout"
    assert v2["spec"]["timeSettings"]["quickRanges"] == QUICK_RANGES
    assert v2["spec"]["timeSettings"]["timezone"] == "Europe/Paris"
    classic = admin("/api/dashboards/uid/bk-qr-c")["dashboard"]
    assert classic["timepicker"]["quick_ranges"] == QUICK_RANGES
    assert (classic["time"], classic["timezone"]) == ({"from": "now-90d", "to": "now-1d"}, "utc")

    # a converter that drops the setting is reported, not shipped silently
    real = publish.to_v2

    def dropping(board, folder_uid=None):
        res = real(board, folder_uid)
        res["spec"]["timeSettings"].pop("quickRanges", None)
        return res

    monkeypatch.setattr(publish, "to_v2", dropping)
    [r2] = deploy(decl, inventory=inv)
    assert [p for p in r2["problems"]
            if p.startswith("bk-qr-t: time setting quickRanges: read back None")], r2["problems"]
    assert not [p for p in r2["problems"] if "bk-qr-c" in p], "the classic board is unaffected"


def _query(g: Grafana, ds_uid: str, sql: str, org_id: int | None = None):
    go = g.in_org(org_id) if org_id else g
    r = go("/api/ds/query", "POST", {"queries": [{"refId": "A", "datasource": {"uid": ds_uid},
                                                  "rawSql": sql, "format": "table"}],
                                     "from": "now-1h", "to": "now"})
    frame = r["results"]["A"]["frames"][0]
    return sorted(frame["data"]["values"][0])


def test_isolated_org_journey(tmp_path, tmp_inventory, grafana_container, monkeypatch):
    """A sensitive project whose client may read only their rows: the board is
    ALSO published into an org of the client's own, through a database role that
    sees only their rows. boardkit makes the orgs, boards and datasources; who is
    in which org is the operator's, set here in Grafana's own API.

    deploy -> orgs, folders, boards and datasources exist -> the operator seats
    the client in their org only, and the internal viewer in the project's org
    -> the client's datasource returns ONLY their rows and they reach nothing of
    the other org -> deploying again leaves the seating alone."""
    c = grafana_container
    admin = _admin(c)
    monkeypatch.setenv("E2E_PG_ALL", c["pg_passwords"]["ro_all"])
    monkeypatch.setenv("E2E_PG_A", c["pg_passwords"]["ro_a"])
    inv_path = tmp_path / "hosts.toml"
    inv_path.write_text(tmp_inventory.read_text() + f'''
[hosts.local.datasource_defs.rho_all]
type = "grafana-postgresql-datasource"
url = "{c['pg_host']}"
database = "book"
user = "ro_all"
password_var = "E2E_PG_ALL"
sslmode = "disable"
[hosts.local.datasource_defs.rho_a]
type = "grafana-postgresql-datasource"
url = "{c['pg_host']}"
database = "book"
user = "ro_a"
password_var = "E2E_PG_A"
sslmode = "disable"
''')
    inv = load_inventory(inv_path)
    decl = _write(tmp_path, {
        "project": "rho", "audience": "internal", "sensitivity": "sensitive",
        "datasources": {"${DS_MAIN}": "rho_all"},
        "folders": [{"uid": "rho", "title": "Rho", "org": "Rho",
                     "boards": [
                         {"file": "r1.json", "org": "Rho · a",
                          "datasources": {"${DS_MAIN}": "rho_a"}},
                         {"file": "r2.json"}]}]},
        {"r1.json": plain("rho-r1", "Client A", "${DS_MAIN}"),
         "r2.json": plain("rho-r2", "Internal", "${DS_MAIN}")})

    [r] = deploy(decl, inventory=inv)
    assert r["problems"] == [], r["problems"]
    assert sorted((p["uid"], p["org"]) for p in r["published"]) == \
        [("rho-r1", "Rho"), ("rho-r1", "Rho · a"), ("rho-r2", "Rho")]
    orgs = {o["name"]: o["id"] for o in admin("/api/orgs")}

    # the operator seats people: the client in their org only, ops in the project's
    for email, org in (("cli@example.com", "Rho · a"), ("ops@example.com", "Rho")):
        _ui(c, "/api/admin/users", "POST", {"name": email, "email": email, "login": email,
                                            "password": PASSWORD, "OrgId": orgs[org]})
    seated = {o: sorted(u["login"] for u in _ui(c, f"/api/orgs/{i}/users"))
              for o, i in orgs.items() if o.startswith("Rho")}

    u = _as(c, "cli@example.com")
    assert u("/api/user")["orgId"] == orgs["Rho · a"], "the client lands in their own org"
    assert {o["name"] for o in u("/api/user/orgs")} == {"Rho · a"}, "and in no other"
    assert _status(u, "/api/dashboards/uid/rho-r1") == 200
    assert _status(u, "/api/dashboards/uid/rho-r2") == 404, "not in their org at all"
    assert _query(u, "bk-rho-a", "select account from book") == ["a", "a"], \
        "their datasource returns only their rows"
    for other in (orgs["Rho"], 1):               # an org they are not in: nothing of it
        go = u.in_org(other)
        try:
            seen = {d["uid"] for d in go("/api/search?type=dash-db")}
        except GrafanaError as e:
            assert e.status in (401, 403), e
            seen = set()
        assert seen <= {"rho-r1"}, f"cli sees {seen} via org {other}"
        with pytest.raises(GrafanaError) as denied:
            _query(u, "bk-rho-all", "select account from book", other)
        assert denied.value.status in (400, 401, 403, 404), denied.value
    ops = _as(c, "ops@example.com")
    assert _query(ops, "bk-rho-all", "select account from book", orgs["Rho"]) == ["a", "a", "b"]
    assert {d["uid"] for d in ops.in_org(orgs["Rho"])("/api/search?type=dash-db")} == \
        {"rho-r1", "rho-r2"}

    # deploying again leaves the seating alone
    [r2] = deploy(decl, inventory=inv)
    assert r2["problems"] == [] and r2["changed"] == [], r2
    assert {o: sorted(x["login"] for x in _ui(c, f"/api/orgs/{i}/users"))
            for o, i in orgs.items() if o.startswith("Rho")} == seated

    # the audit's main-org views describe the main org whichever org the admin
    # last switched to (an unheaded call acts in the CURRENT org: it once read 3)
    from boardkit.audit import audit
    admin("/api/user/using/" + str(orgs["Rho"]), "POST")
    try:
        a = audit(admin)
        assert "bk-rho-all" not in " ".join(a["datasources"]), a["datasources"]
        assert not any("rho" in f for f in a["folders"]), a["folders"]
    finally:
        admin("/api/user/using/1", "POST")
