"""Put one board on a Grafana and prove it landed.

Publishing is: the folder exists (by uid), the board goes up (classic, or v2
with real tabs), the values a viewer chose in the dropdowns survive, and the
board is read BACK and compared with what was sent. A 200 is not a deploy.
"""
from __future__ import annotations

import copy

from .grafana import Grafana, GrafanaError, q
from .v2 import API_VERSION, to_v2
from .v2 import tabs as v2_tabs


def ensure_folder(g: Grafana, uid: str, title: str) -> list[str]:
    """Create the folder by uid, or retitle it. Returns what changed."""
    got = g(f"/api/folders/{q(uid)}", ok404=True)
    if got is None:
        g("/api/folders", "POST", {"uid": uid, "title": title})
        return [f"created folder {title!r} ({uid})"]
    if got.get("title") != title:
        g(f"/api/folders/{q(uid)}", "PUT",
          {"title": title, "version": got.get("version"), "overwrite": True})
        return [f"retitled folder {uid}: {got.get('title')!r} -> {title!r}"]
    return []


def carry_over_variables(g: Grafana, board: dict, only=None) -> list[str]:
    """Carry each variable's DEPLOYED current value into `board` (in place).

    Republishing a board must not reset what a viewer set in its dropdowns or
    text boxes. A custom variable whose option list no longer holds the
    deployed value falls back to the generated default; the VALUE is the
    viewer's, the option LABEL is code. `only` limits it to those names (a
    deliberate reset keeps the viewer's own preferences).
    """
    uid = board.get("uid")
    live = g(f"/api/dashboards/uid/{q(uid)}", ok404=True) if uid else None
    if not live:
        return []
    deployed = {v["name"]: v for v in
                live.get("dashboard", {}).get("templating", {}).get("list", [])}
    kept = []
    for var in board.get("templating", {}).get("list", []):
        if only is not None and var["name"] not in only:
            continue
        was = deployed.get(var["name"])
        if not was or "current" not in was:
            continue
        value = was["current"].get("value")
        if var.get("type") == "textbox":
            var["options"] = [{"selected": True, "text": was["current"].get("text", value),
                               "value": value}]
        else:
            legal = {o.get("value") for o in var.get("options", [])}
            if value not in legal:
                continue
            for opt in var["options"]:
                opt["selected"] = opt.get("value") == value
        if value == var.get("current", {}).get("value"):
            continue
        var["current"] = dict(was["current"])
        if var.get("type") != "textbox":
            var["current"]["text"] = next(o["text"] for o in var["options"]
                                          if o.get("value") == value)
        kept.append(f"{var['name']}={value}")
    return kept


def _flat_titles(panels: list[dict]) -> list[str]:
    out = []
    for p in panels:
        out.append(p.get("title", ""))
        out.extend(_flat_titles(p.get("panels", [])))
    return out


def _put_classic(g: Grafana, board: dict, folder_uid: str, message: str) -> list[str]:
    b = copy.deepcopy(board)
    b["id"] = None
    try:
        g("/api/dashboards/db", "POST", {"dashboard": b, "folderUid": folder_uid,
                                         "overwrite": True, "message": message})
    except GrafanaError as e:
        if "provisioned" in e.body.lower():
            raise GrafanaError("POST", "/api/dashboards/db", e.status,
                               f"{board['uid']} is file-provisioned on this host (a mirror "
                               "sync owns it): take its folder out of MIRROR_FOLDERS first") \
                from None
        raise
    got = g(f"/api/dashboards/uid/{q(board['uid'])}")
    problems = []
    if got["meta"].get("folderUid") != folder_uid:
        problems.append(f"in folder {got['meta'].get('folderUid')!r}, not {folder_uid!r}")
    if _flat_titles(got["dashboard"].get("panels", [])) != _flat_titles(board.get("panels", [])):
        problems.append("panels read back differ from the panels sent")
    return problems


def _put_v2(g: Grafana, board: dict, folder_uid: str) -> list[str]:
    ns = g.namespace()
    base = f"/apis/{API_VERSION}/namespaces/{ns}/dashboards"
    res = to_v2(board, folder_uid)
    name = res["metadata"]["name"]
    live = g(f"{base}/{q(name)}", ok404=True)
    if live is None:
        g(base, "POST", res)
    else:
        # the live resourceVersion: a concurrent edit fails instead of being lost
        res["metadata"]["resourceVersion"] = live["metadata"]["resourceVersion"]
        g(f"{base}/{q(name)}", "PUT", res)
    got = g(f"{base}/{q(name)}")
    problems = []
    if got.get("status", {}).get("conversion", {}).get("failed"):
        problems.append("v2 conversion failed on read-back")
    if got["metadata"].get("annotations", {}).get("grafana.app/folder") != folder_uid:
        problems.append(f"not in folder {folder_uid!r}")
    layout = got["spec"]["layout"]
    has_rows = any(p["type"] == "row" for p in board["panels"])
    if has_rows:
        want = [t for t, _, _ in v2_tabs(board)]
        have = ([t["spec"]["title"] for t in layout["spec"].get("tabs", [])]
                if layout["kind"] == "TabsLayout" else None)
        if have != want:
            problems.append(f"tabs {have} != {want}")
        n = sum(len(p) for _, _, p in v2_tabs(board))
    else:
        n = len(board["panels"])
    if len(got["spec"]["elements"]) != n:
        problems.append(f"{len(got['spec']['elements'])} panels read back, sent {n}")
    return problems


def publish_board(g: Grafana, board: dict, folder_uid: str, *, tabs: bool = False,
                  reset_vars: bool = False, preference_vars=(), message: str = "boardkit"
                  ) -> dict:
    """Publish one (already datasource-rewired) classic board. Returns
    {"uid", "kept", "problems"}; `problems` empty means it read back right."""
    board = copy.deepcopy(board)
    board.pop("id", None)
    kept = carry_over_variables(g, board, only=set(preference_vars) if reset_vars else None)
    problems = (_put_v2(g, board, folder_uid) if tabs
                else _put_classic(g, board, folder_uid, message))
    return {"uid": board["uid"], "kept": kept, "problems": problems}
