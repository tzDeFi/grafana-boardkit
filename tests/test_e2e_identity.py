"""Nobody can take a declared person's seat by NAME (real Grafana OSS 13.0.2).

Grafana logins and emails are claims their holder controls: an invitee picks
their own username, and anyone signed in can rename both (PUT /api/user) to
any unused value. A 2026-09-28 review reproduced both attacks against boardkit
0.2.2: a viewer renamed to the address of a declared-but-unregistered person
was seated in that person's isolated org and read their rows; a later rename
crashed every apply (409) and stalled every project on the host.

boardkit now creates the account of every declared address at once (so the
address is never free), pins it by id, and acts only on that id.
"""
from __future__ import annotations

import json

import pytest

from boardkit.deploy import apply_stored, deploy
from boardkit.grafana import Grafana, GrafanaError
from boardkit.hosts import load_inventory

from .boards import plain

pytestmark = pytest.mark.e2e


def _inventory(tmp_path, tmp_inventory, c, monkeypatch):
    monkeypatch.setenv("E2E_PG_ALL", c["pg_passwords"]["ro_all"])
    monkeypatch.setenv("E2E_PG_A", c["pg_passwords"]["ro_a"])
    p = tmp_path / "hosts.toml"
    p.write_text(tmp_inventory.read_text() + "".join(f'''
[hosts.local.datasource_defs.{name}]
type = "grafana-postgresql-datasource"
url = "{c['pg_host']}"
database = "book"
user = "{user}"
password_var = "{var}"
sslmode = "disable"
''' for name, user, var in (("rho_all", "ro_all", "E2E_PG_ALL"), ("rho_a", "ro_a", "E2E_PG_A"))))
    monkeypatch.setenv("BOARDKIT_HOSTS", str(p))
    return load_inventory(p)


def _decl(tmp_path, project, victim, other):
    d = {"project": project, "audience": "internal", "sensitivity": "sensitive",
         "datasources": {"${DS_MAIN}": "rho_all"},
         "folders": [{"uid": project, "title": project.title(), "org": project.title(),
                      "viewers": [f"ops-{project}@example.com"],
                      "boards": [
                          {"file": "v.json", "viewers": [victim], "org": f"{project} · v",
                           "datasources": {"${DS_MAIN}": "rho_a"}},
                          {"file": "o.json", "viewers": [other], "org": f"{project} · o",
                           "datasources": {"${DS_MAIN}": "rho_all"}}]}]}
    (tmp_path / "v.json").write_text(json.dumps(plain(f"{project}-v", "Victim", "${DS_MAIN}")))
    (tmp_path / "o.json").write_text(json.dumps(plain(f"{project}-o", "Other", "${DS_MAIN}")))
    p = tmp_path / "boards.json"
    p.write_text(json.dumps(d))
    return p


def _orgs(admin):
    return {o["name"]: o["id"] for o in admin("/api/orgs")}


def test_a_declared_address_is_never_free_to_squat(tmp_path, tmp_inventory, grafana_container,
                                                   monkeypatch):
    c = grafana_container
    inv = _inventory(tmp_path, tmp_inventory, c, monkeypatch)
    admin = Grafana(c["url"], user="admin", password=c["password"])
    decl = _decl(tmp_path, "sq", "victim@example.com", "mallory@example.com")
    [r] = deploy(decl, inventory=inv)
    assert r["problems"] == [], r
    assert {"victim@example.com", "mallory@example.com"} <= set(r["new_accounts"])
    mallory = Grafana(c["url"], user="mallory@example.com",
                      password=r["new_accounts"]["mallory@example.com"])
    # both renames the review used are refused: the victim's login and email exist
    for body in ({"login": "victim@example.com", "email": "mallory@example.com", "name": "m"},
                 {"login": "mallory@example.com", "email": "victim@example.com", "name": "m"}):
        with pytest.raises(GrafanaError):
            mallory("/api/user", "PUT", body)
    [r2] = deploy(decl, inventory=inv, access_only=True)
    assert r2["problems"] == [] and r2["changed"] == []
    victim_org = _orgs(admin)["sq · v"]
    members = {m["login"] for m in admin(f"/api/orgs/{victim_org}/users")}
    assert members == {"admin", "victim@example.com"}, members
    assert "sq · v" not in {o["name"] for o in mallory("/api/user/orgs")}


def test_a_renamed_pinned_account_loses_its_seat_and_nothing_crashes(
        tmp_path, tmp_inventory, grafana_container, monkeypatch):
    """The live situation: an existing viewer renames to someone's unused
    address, then to anything at all. They are unseated, reported, and every
    later apply of every project still converges."""
    c = grafana_container
    inv = _inventory(tmp_path, tmp_inventory, c, monkeypatch)
    admin = Grafana(c["url"], user="admin", password=c["password"])
    decl = _decl(tmp_path, "rn", "adam@example.com", "seb@example.com")
    [r] = deploy(decl, inventory=inv)
    seb = Grafana(c["url"], user="seb@example.com", password=r["new_accounts"]["seb@example.com"])
    seb("/api/user", "PUT", {"login": "future@example.com", "email": "future@example.com",
                             "name": "seb"})
    seb = Grafana(c["url"], user="future@example.com",
                  password=r["new_accounts"]["seb@example.com"])
    for _ in range(3):
        reports = apply_stored(inv.hosts["local"], None, access_only=True)
        rn = next(x for x in reports if x["project"] == "rn")
        assert not any("apply failed" in p for x in reports for p in x["problems"]), reports
    assert any("seb@example.com: pinned account" in p and "not seated" in p
               for p in rn["problems"]), rn["problems"]
    orgs = _orgs(admin)
    assert "future@example.com" not in {m["login"] for m in
                                        admin(f"/api/orgs/{orgs['rn · o']}/users")}
    assert "rn · v" not in {o["name"] for o in seb("/api/user/orgs")}
    # and the address it grabbed is NOT trusted when someone later declares it
    decl2 = _decl(tmp_path, "rn", "adam@example.com", "future@example.com")
    [r2] = deploy(decl2, inventory=inv, access_only=True)
    assert any(p.startswith("future@example.com: already held by an account boardkit did not "
                            "create") for p in r2["problems"]), r2["problems"]


def test_one_projects_failure_never_stalls_the_others(tmp_path, tmp_inventory,
                                                      grafana_container, monkeypatch):
    c = grafana_container
    inv = _inventory(tmp_path, tmp_inventory, c, monkeypatch)
    deploy(_decl(tmp_path, "ok", "ok-v@example.com", "ok-o@example.com"), inventory=inv)
    broken = inv.hosts["local"].store_path / "projects" / "broken"
    broken.mkdir(parents=True)
    (broken / "declaration.json").write_text("{not json")
    reports = {r["project"]: r for r in apply_stored(inv.hosts["local"], None, access_only=True)}
    assert reports["broken"]["problems"], "the broken one is reported"
    assert reports["ok"]["problems"] == [], "and the others still apply"
