"""Orgs and the datasources boardkit creates in them.

A Grafana org is the only boundary Grafana OSS draws around datasources: any
member of an org may POST hand-written SQL to any datasource of that org. So a
sensitive audience gets an org of its own, holding its board and a datasource
that connects as a database role seeing only that audience's rows.
"""
from __future__ import annotations

from .grafana import Grafana, q

MAIN_ORG_ID = 1
DS_PREFIX = "bk-"


def ensure_org(g: Grafana, name: str, log: list[str]) -> int:
    got = g(f"/api/orgs/name/{q(name)}", ok404=True)
    if got:
        return got["id"]
    oid = g("/api/orgs", "POST", {"name": name})["orgId"]
    log.append(f"created org {name!r}")
    return oid


def org_members(g: Grafana, oid: int) -> dict[str, dict]:
    return {u["email"].lower(): u for u in g(f"/api/orgs/{oid}/users")}


def datasource_uid(logical: str) -> str:
    return (DS_PREFIX + logical.replace("_", "-"))[:40]


def ensure_datasource(g_org: Grafana, logical: str, spec: dict, env: dict, log: list[str],
                      org_name: str) -> dict:
    """Create or update datasource `bk-<logical>` in the org g_org acts in, from
    the host inventory's definition. The password comes from the host's env."""
    pw_var = spec.get("password_var")
    pw = env.get(pw_var) if pw_var else None
    if pw_var and not pw:
        raise SystemExit(f"{pw_var} (datasource {logical}) is not set on this host")
    uid = datasource_uid(logical)
    body = {"uid": uid, "name": logical, "type": spec["type"], "access": "proxy",
            "url": spec.get("url", ""), "user": spec.get("user", ""),
            "jsonData": {"database": spec.get("database", ""),
                         "sslmode": spec.get("sslmode", "require"),
                         **spec.get("json_data", {})},
            "secureJsonData": {"password": pw} if pw else {}}
    have = g_org(f"/api/datasources/uid/{q(uid)}", ok404=True)
    if have is None:
        g_org("/api/datasources", "POST", body)
        log.append(f"created datasource {logical!r} in {org_name!r}")
    else:
        g_org(f"/api/datasources/uid/{q(uid)}", "PUT", {**body, "id": have["id"],
                                                        "version": have.get("version")})
    return {"type": spec["type"], "uid": uid}
