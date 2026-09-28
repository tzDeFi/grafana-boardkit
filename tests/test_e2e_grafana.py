"""The whole journey on a real Grafana OSS 13.0.2 (docker), as a repo lives it.

  a sensitive project declares a folder (internal viewer) with two boards,
  each with its own viewer -> deploy: folder + boards (one with real tabs),
  datasources rewired, invites issued -> the invitees REGISTER through
  Grafana's own invite flow -> the host's access-only apply seats them ->
  each logs in and can open exactly what was declared, home page included ->
  anonymous gets nothing -> an open project next to it is visible to every
  account but grants nothing on the sensitive folder -> removing a viewer
  from the declaration takes the access away.

And the converter is pinned to Grafana's own classic -> v2 conversion, live.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

import pytest

from boardkit.deploy import deploy
from boardkit.grafana import Grafana, GrafanaError
from boardkit.hosts import load_inventory
from boardkit.v2 import to_v2

from .boards import plain, tabbed

pytestmark = pytest.mark.e2e


def _admin(c):
    return Grafana(c["url"], user="admin", password=c["password"])


def _as(c, email, pw="Correct-horse-9"):
    return Grafana(c["url"], user=email, password=pw)


def _status(g: Grafana, path: str) -> int:
    try:
        g(path)
        return 200
    except GrafanaError as e:
        return e.status


#: Grafana caches a user's resolved permissions (~60 s): a grant or a removal
#: reaches an account that already made requests within that window. Measured
#: waits are recorded so a slower Grafana shows up in the test output.
WAITS: dict[str, float] = {}


def _eventually(g: Grafana, path: str, want: int, label: str, timeout: float = 90) -> None:
    t0 = time.time()
    while True:
        got = _status(g, path)
        if got == want:
            WAITS[label] = round(time.time() - t0, 1)
            return
        assert time.time() - t0 < timeout, f"{label}: still {got} after {timeout}s"
        time.sleep(2)


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


SENSITIVE = {
    "project": "alpha", "audience": "internal", "sensitivity": "sensitive",
    "datasources": {"${DS_MAIN}": "main", "maindb": "main"},
    "folders": [{
        "uid": "bk-alpha", "title": "Alpha", "viewers": ["int@example.com"], "tabs": True,
        "boards": [
            {"file": "a.json", "viewers": ["alice@example.com"], "home": True,
             "preference_vars": ["loss"]},
            {"file": "b.json", "viewers": ["bob@example.com"], "tabs": False}]}]}


def _register(c, link: str, email: str):
    code = link.rsplit("/", 1)[1]
    body = json.dumps({"inviteCode": code, "email": email, "username": email,
                       "name": email.split("@")[0], "password": "Correct-horse-9",
                       "confirmPassword": "Correct-horse-9"}).encode()
    req = urllib.request.Request(c["url"] + "/api/user/invite/complete", data=body,
                                 method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        assert r.status == 200


def test_journey_sensitive_and_open_projects(tmp_path, tmp_inventory, grafana_container,
                                             datasource):
    c = grafana_container
    inv = load_inventory(tmp_inventory)
    admin = _admin(c)
    decl = _write(tmp_path, SENSITIVE, {"a.json": tabbed("bk-a"), "b.json": plain("bk-b")})

    # 1. first deploy: boards up, invites out, nobody seated yet
    [r] = deploy(decl, inventory=inv)
    assert r["problems"] == [], r
    assert {p["uid"] for p in r["published"]} == {"bk-a", "bk-b"}
    assert set(r["invites"]) == {"int@example.com", "alice@example.com", "bob@example.com"}
    ns = admin.namespace()
    a = admin(f"/apis/dashboard.grafana.app/v2/namespaces/{ns}/dashboards/bk-a")
    assert a["spec"]["layout"]["kind"] == "TabsLayout"
    assert [t["spec"]["title"] for t in a["spec"]["layout"]["spec"]["tabs"]] == \
        ["Performance", "Risk"]
    b = admin("/api/dashboards/uid/bk-b")["dashboard"]
    assert b["panels"][0]["datasource"] == {"type": "grafana-postgresql-datasource",
                                            "uid": "pg-main"}, "rewired to the host's uid"

    # 2. they register through Grafana's own invite flow
    for email, link in r["invites"].items():
        _register(c, link, email)

    # 3. the host's timer (access-only apply) seats them
    [r2] = deploy(decl, inventory=inv, access_only=True)
    assert r2["problems"] == [] and r2["invites"] == {}
    assert sum("added" in x for x in r2["changed"]) == 3

    # 4. each sees exactly what was declared
    alice, bob, internal = (_as(c, e) for e in
                            ("alice@example.com", "bob@example.com", "int@example.com"))
    assert (_status(alice, "/api/dashboards/uid/bk-a"),
            _status(alice, "/api/dashboards/uid/bk-b")) == (200, 403)
    assert (_status(bob, "/api/dashboards/uid/bk-a"),
            _status(bob, "/api/dashboards/uid/bk-b")) == (403, 200)
    assert (_status(internal, "/api/dashboards/uid/bk-a"),
            _status(internal, "/api/dashboards/uid/bk-b")) == (200, 200)
    assert {d["uid"] for d in alice("/api/search?type=dash-db")} == {"bk-a"}
    home = alice("/api/dashboards/home")
    assert home.get("redirectUri", "").startswith("/d/bk-a/"), home
    with pytest.raises(urllib.error.HTTPError) as anon:
        urllib.request.urlopen(c["url"] + "/api/search", timeout=5)
    assert anon.value.code == 401

    # 5. an open project on the same host: every account sees it, and it grants
    #    nothing on the sensitive folder
    (tmp_path / "open").mkdir()
    open_decl = _write(tmp_path / "open",
                       {"project": "beta", "audience": "internal", "sensitivity": "open",
                        "datasources": {"${DS_MAIN}": "main"},
                        "folders": [{"uid": "bk-open", "title": "Open", "viewers": "all",
                                     "boards": [{"file": "o.json"}]}]},
                       {"o.json": plain("bk-o", "Open board", "${DS_MAIN}")})
    [ro] = deploy(open_decl, inventory=inv)
    assert ro["problems"] == []
    admin("/api/admin/users", "POST", {"name": "carol", "email": "carol@example.com",
                                       "login": "carol@example.com",
                                       "password": "Correct-horse-9"})
    carol = _as(c, "carol@example.com")
    assert {d["uid"] for d in carol("/api/search?type=dash-db")} == {"bk-o"}
    _eventually(alice, "/api/dashboards/uid/bk-o", 200, "grant reaches a cached account")

    # 6. republishing is idempotent (v2 PUT on the live resourceVersion)
    [r3] = deploy(decl, inventory=inv)
    assert r3["problems"] == [] and not [x for x in r3["changed"] if "team" in x]

    # 7. bob leaves the board's group (dan replaces him): bob loses the board
    SENS2 = json.loads(json.dumps(SENSITIVE))
    SENS2["folders"][0]["boards"][1]["viewers"] = ["dan@example.com"]
    decl2 = _write(tmp_path, SENS2, {})
    [r4] = deploy(decl2, inventory=inv, access_only=True)
    assert "removed bob@example.com from 'alpha · bk-b'" in r4["changed"], r4["changed"]
    assert set(r4["invites"]) == {"dan@example.com"}
    _eventually(bob, "/api/dashboards/uid/bk-b", 403, "removal reaches the removed viewer")
    assert _status(alice, "/api/dashboards/uid/bk-a") == 200, "nobody else affected"

    # 8. the board keeps no viewers of its own: its grant goes, the team is
    #    reported stale (never deleted automatically)
    SENS2["folders"][0]["boards"][1]["viewers"] = []
    decl3 = _write(tmp_path, SENS2, {})
    [r5] = deploy(decl3, inventory=inv, access_only=True)
    assert "removed team 'alpha · bk-b' from bk-b" in r5["changed"], r5["changed"]
    assert any("alpha · bk-b" in w for w in r5["warnings"]), "stale team reported"
    assert _status(internal, "/api/dashboards/uid/bk-b") == 200, "folder viewers keep it"
    print("permission propagation:", WAITS)


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


@pytest.mark.parametrize("make", [tabbed, plain], ids=["rows", "no-rows"])
def test_converter_matches_grafanas_own_conversion(grafana_container, datasource, make):
    """Store the classic board through the legacy API, read Grafana's v2
    rendering of it, and require to_v2 to produce the same spec (with rows
    swapped for tabs, the one intended difference)."""
    admin = _admin(grafana_container)
    board = make(uid=f"conv-{make.__name__}", ds_ref="pg-main")
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
