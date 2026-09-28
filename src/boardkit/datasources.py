"""Point a board's datasource references at the host's datasources.

A board is written against the datasource names its repo knows (`maindb`,
`${DS_MAIN}`, a cloud uid...). Each host provisions its own datasources under
its own uids. The repo's declaration maps each reference to a LOGICAL name
("main"), the host inventory maps each logical name to a local uid, and
this module rewrites the board. An unmapped reference is an error, never a
silent fallback: a panel quietly querying the wrong database is the worst
outcome a publisher can produce.
"""
from __future__ import annotations

import copy

#: references Grafana resolves itself; never rewired
BUILTIN_UIDS = {"grafana", "-- Grafana --", "-- Mixed --", "-- Dashboard --", "__expr__"}
BUILTIN_TYPES = {"grafana", "datasource", "__expr__"}


def _key(ref) -> str | None:
    if isinstance(ref, str):
        return None if ref in BUILTIN_UIDS else ref
    if isinstance(ref, dict):
        if ref.get("type") in BUILTIN_TYPES or ref.get("uid") in BUILTIN_UIDS:
            return None
        return ref.get("uid") or None
    return None


def references(board: dict) -> set[str]:
    """Every non-builtin datasource reference in a classic board."""
    found: set[str] = set()

    def walk(o):
        if isinstance(o, dict):
            if "datasource" in o and (k := _key(o["datasource"])):
                found.add(k)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(board.get("panels", []))
    walk(board.get("templating", {}))
    walk(board.get("annotations", {}))
    return found


def rewire(board: dict, targets: dict[str, dict]) -> dict:
    """A copy of `board` with every reference in `targets` replaced by
    `{"type": ..., "uid": ...}`. Raises ValueError naming unmapped refs.

    Export-format leftovers go too: `__inputs` / `__requires`, and
    datasource-type template variables whose only job was to be a placeholder.
    """
    b = copy.deepcopy(board)
    for k in ("__inputs", "__requires", "__elements"):
        b.pop(k, None)
    tpl = b.get("templating", {}).get("list")
    if tpl is not None:
        b["templating"]["list"] = [v for v in tpl if v.get("type") != "datasource"]
    missing = references(b) - set(targets)
    if missing:
        raise ValueError(f"board {b.get('uid')!r}: no datasource mapping for "
                         f"{sorted(missing)} (add them to [datasources] in the declaration)")

    def walk(o):
        if isinstance(o, dict):
            if "datasource" in o and (k := _key(o["datasource"])):
                o["datasource"] = dict(targets[k])
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(b.get("panels", []))
    walk(b.get("templating", {}))
    walk(b.get("annotations", {}))
    return b
