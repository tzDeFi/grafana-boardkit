"""Synthetic boards in the shapes real repos emit (no real data, no names)."""
from __future__ import annotations

import copy


def _panel(pid, title, kind, y, ds, sql, x=0, w=12, h=6):
    p = {"id": pid, "title": title, "type": kind,
         "gridPos": {"x": x, "y": y, "w": w, "h": h},
         "datasource": copy.deepcopy(ds), "description": f"{title} (i)",
         "options": {"reduceOptions": {"calcs": ["lastNotNull"]}} if kind == "stat" else {},
         "fieldConfig": {"defaults": {"unit": "percent"}, "overrides": []}}
    if kind != "text":
        p["targets"] = [{"refId": "A", "datasource": copy.deepcopy(ds), "format": "table",
                         "rawQuery": True, "rawSql": sql}]
    else:
        p["options"] = {"mode": "markdown", "content": "notes"}
        del p["datasource"]
    return p


def tabbed(uid="bk-alpha", title="Alpha", ds_ref="${DS_MAIN}") -> dict:
    """Two rows -> two tabs; a custom and a textbox variable."""
    ds = {"type": "postgres", "uid": ds_ref}
    return {
        "uid": uid, "title": title, "tags": ["boardkit-e2e"], "timezone": "utc",
        "graphTooltip": 1, "editable": False, "refresh": "5m", "schemaVersion": 41,
        "time": {"from": "now-7d", "to": "now"},
        "templating": {"list": [
            {"type": "custom", "name": "freq", "label": "Frequency", "query": "1d,1h",
             "current": {"text": "1d", "value": "1d"}, "hide": 0,
             "options": [{"text": "1d", "value": "1d", "selected": True},
                         {"text": "1h", "value": "1h", "selected": False}]},
            {"type": "textbox", "name": "loss", "label": "Loss $k", "query": "40",
             "current": {"text": "40", "value": "40"}, "hide": 0,
             "options": [{"text": "40", "value": "40", "selected": True}]}]},
        "panels": [
            {"id": 100, "type": "row", "title": "Performance", "collapsed": False,
             "gridPos": {"x": 0, "y": 0, "w": 24, "h": 1}, "panels": []},
            _panel(1, "Return", "stat", 1, ds, "select 1 as v"),
            _panel(2, "NAV", "timeseries", 1, ds, "select now() as t, 1 as v", x=12),
            {"id": 101, "type": "row", "title": "Risk", "collapsed": False,
             "gridPos": {"x": 0, "y": 7, "w": 24, "h": 1}, "panels": []},
            _panel(3, "Exposure", "table", 8, ds, "select 'x' as coin, 2 as usd"),
            _panel(4, "Notes", "text", 14, ds, ""),
        ],
    }


def plain(uid="bk-beta", title="Beta", ds_ref="maindb") -> dict:
    """No rows: one grid, published classic."""
    ds = {"type": "postgres", "uid": ds_ref}
    return {"uid": uid, "title": title, "tags": [], "schemaVersion": 41,
            "time": {"from": "now-24h", "to": "now"}, "templating": {"list": []},
            "panels": [_panel(1, "Volume", "timeseries", 0, ds, "select now() as t, 3 as v")]}
