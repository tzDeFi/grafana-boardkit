"""Classic board JSON -> Grafana v2 dashboard resource, with REAL tabs.

Boards are generated and committed as classic JSON: every test and audit walks
that shape. Real tabs only exist in the v2 schema (`TabsLayout`), so publishing
converts at the last step. Each classic ROW becomes one tab, in order, carrying
its panels' grid positions relative to the row. A board without rows stays one
plain grid.

The mapping is Grafana's own server-side v1 -> v2 conversion (pinned against a
live grafana-oss 13.0.2 by tests/test_e2e_grafana.py), for the subset boards
use: stat / timeseries / table / text / bar / gauge panels, raw-SQL targets,
custom and textbox variables. Anything outside that subset raises rather than
converting half a panel; publish such a board classic (`tabs = false`).
"""
from __future__ import annotations

import copy

API_VERSION = "dashboard.grafana.app/v2"

#: classic datasource `type` -> v2 DataQuery `group`. The classic JSON says
#: "postgres"; Grafana stores the plugin id.
_GROUPS = {"postgres": "grafana-postgresql-datasource",
           "grafana-postgresql-datasource": "grafana-postgresql-datasource"}
_PANEL_KEYS = {"id", "title", "type", "gridPos", "options", "datasource",
               "description", "fieldConfig", "targets"}
_TARGET_KEYS = {"datasource", "format", "rawQuery", "rawSql", "refId"}
_HIDE = {0: "dontHide", 1: "hideLabel", 2: "hideVariable"}
_REFRESH_INTERVALS = ["5s", "10s", "30s", "1m", "5m", "15m", "30m", "1h",
                      "2h", "1d"]
_CURSOR = {0: "Off", 1: "Crosshair", 2: "Tooltip"}

BUILTIN_ANNOTATION = {
    "kind": "AnnotationQuery",
    "spec": {"query": {"kind": "DataQuery", "group": "grafana",
                       "version": "v0",
                       "datasource": {"name": "-- Grafana --"}, "spec": {}},
             "enable": True, "hide": True,
             "iconColor": "rgba(0, 211, 255, 1)",
             "name": "Annotations & Alerts", "builtIn": True,
             "legacyOptions": {"type": "dashboard"}}}


def _query(target: dict, panel_ds: dict | None) -> dict:
    extra = set(target) - _TARGET_KEYS
    if extra:
        raise ValueError(f"target keys the converter does not map: {extra}")
    ds = target.get("datasource") or panel_ds or {}
    t = ds.get("type", "")
    group = _GROUPS.get(t) or (t if t.endswith("-datasource") else None)
    if group is None:
        raise ValueError(f"unmapped datasource type {ds.get('type')!r}")
    spec = {k: v for k, v in target.items()
            if k not in ("datasource", "refId")}
    return {"kind": "PanelQuery",
            "spec": {"query": {"kind": "DataQuery", "group": group,
                               "version": "v0",
                               "datasource": {"name": ds["uid"]},
                               "spec": spec},
                     "refId": target["refId"], "hidden": False}}


def _panel(p: dict) -> dict:
    extra = set(p) - _PANEL_KEYS
    if extra:
        raise ValueError(f"panel {p.get('id')}: keys the converter does not "
                         f"map: {extra}")
    fc = copy.deepcopy(p.get("fieldConfig", {"defaults": {}, "overrides": []}))
    # Grafana drops a null base-step value inside overrides (not in defaults)
    for o in fc.get("overrides", []):
        for prop in o.get("properties", []):
            val = prop.get("value")
            if isinstance(val, dict) and isinstance(val.get("steps"), list):
                for step in val["steps"]:
                    if step.get("value", 0) is None:
                        del step["value"]
    vizspec = {"options": copy.deepcopy(p.get("options", {})),
               "fieldConfig": fc}
    return {"kind": "Panel",
            "spec": {"id": p["id"], "title": p["title"],
                     "description": p.get("description", ""), "links": [],
                     "data": {"kind": "QueryGroup",
                              "spec": {"queries": [
                                  _query(t, p.get("datasource"))
                                  for t in p.get("targets", [])],
                                  "transformations": [],
                                  "queryOptions": {}}},
                     "vizConfig": {"kind": "VizConfig", "group": p["type"],
                                   "version": "", "spec": vizspec}}}


def _variable(v: dict) -> dict:
    common = {"name": v["name"], "label": v.get("label", ""),
              "hide": _HIDE[v.get("hide", 0)],
              "skipUrlSync": v.get("skipUrlSync", False)}
    if "description" in v:          # Grafana omits it when the classic board has none
        common["description"] = v["description"]
    cur = {"text": v["current"]["text"], "value": v["current"]["value"]}
    if v["type"] == "custom":
        return {"kind": "CustomVariable",
                "spec": {"name": v["name"], "query": v["query"],
                         "current": cur,
                         "options": copy.deepcopy(v["options"]),
                         "multi": v.get("multi", False),
                         "includeAll": v.get("includeAll", False),
                         **{k: common[k] for k in
                            ("label", "hide", "skipUrlSync", "description") if k in common},
                         "allowCustomValue": True}}
    if v["type"] == "textbox":
        return {"kind": "TextVariable",
                "spec": {"name": v["name"], "current": cur,
                         "query": v["query"],
                         **{k: common[k] for k in
                            ("label", "hide", "skipUrlSync", "description") if k in common}}}
    raise ValueError(f"variable {v['name']}: unmapped type {v['type']!r}")


def _grid(items: list[dict], row_y: int) -> dict:
    return {"kind": "GridLayout", "spec": {"items": [
        {"kind": "GridLayoutItem",
         "spec": {"x": p["gridPos"]["x"], "y": p["gridPos"]["y"] - row_y,
                  "width": p["gridPos"]["w"], "height": p["gridPos"]["h"],
                  "element": {"kind": "ElementReference",
                              "name": f"panel-{p['id']}"}}}
        for p in sorted(items, key=lambda p: (p["gridPos"]["y"],
                                              p["gridPos"]["x"]))]}}


def tabs(board: dict) -> list[tuple[str, int, list[dict]]]:
    """(title, row y, panels) per classic row, collapsed or not."""
    out: list[tuple[str, int, list[dict]]] = []
    for p in board["panels"]:
        if p["type"] == "row":
            out.append((p["title"], p["gridPos"]["y"] + 1,
                        list(p.get("panels", []))))
        elif not out:
            raise ValueError("panel above the first row: every panel must "
                             "sit in a tab")
        else:
            out[-1][2].append(p)
    return out


def to_v2(board: dict, folder_uid: str | None = None) -> dict:
    """The v2 resource for a classic board. Rows -> tabs, first tab open; a
    board with no rows at all (the aggregate) stays one plain grid."""
    elements: dict[str, dict] = {}

    def add(panels):
        for p in panels:
            key = f"panel-{p['id']}"
            if key in elements:
                raise ValueError(f"duplicate panel id {p['id']}")
            elements[key] = _panel(p)

    if not any(p["type"] == "row" for p in board["panels"]):
        add(board["panels"])
        layout = _grid(board["panels"], 0)
    else:
        layout_tabs = []
        for title, row_y, panels in tabs(board):
            add(panels)
            layout_tabs.append({"kind": "TabsLayoutTab",
                                "spec": {"title": title,
                                         "layout": _grid(panels, row_y)}})
        layout = {"kind": "TabsLayout", "spec": {"tabs": layout_tabs}}
    meta = {"name": board["uid"]}
    if folder_uid:
        meta["annotations"] = {"grafana.app/folder": folder_uid}
    return {
        "apiVersion": API_VERSION,
        "kind": "Dashboard",
        "metadata": meta,
        "spec": {
            "annotations": [copy.deepcopy(BUILTIN_ANNOTATION)],
            "cursorSync": _CURSOR[board.get("graphTooltip", 0)],
            "editable": board.get("editable", True),
            "elements": elements,
            "layout": layout,
            "links": [],
            "liveNow": False,
            "preload": False,
            "tags": list(board.get("tags", [])),
            "timeSettings": {
                **({"timezone": board["timezone"]} if "timezone" in board else {}),
                "from": board["time"]["from"], "to": board["time"]["to"],
                "autoRefresh": board.get("refresh", ""),
                "autoRefreshIntervals": list(_REFRESH_INTERVALS),
                "hideTimepicker": False, "fiscalYearStartMonth": 0},
            "title": board["title"],
            "variables": [_variable(v)
                          for v in board.get("templating", {}).get("list", [])],
        },
    }
