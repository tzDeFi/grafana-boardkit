"""What audience and sensitivity mean, as checks.

Audience picks the host: a board for David alone lives on his own machine, a
board for anyone else on the team host. Sensitivity picks the rules:

  sensitive   the host must require login (no anonymous role at all);
  open        no extra rule.

Who may open a board is Grafana's to decide, not boardkit's (see decl.py), so
no rule here is about people.

One risk no permission can close on Grafana OSS: any org member can POST
/api/ds/query with hand-written SQL to any datasource in the org. So whoever
adds someone to an org adds them to its datasources. The fix is an org per
audience (datasources are per org) with a database role that sees only that
audience's rows: `org` on a folder or board in the declaration. `boardkit
audit` lists every org's members next to its datasources.
"""
from __future__ import annotations

import re

from .decl import Declaration


def host_errors(decl: Declaration, host, settings: dict) -> list[str]:
    """Against the host's EFFECTIVE settings (/api/admin/settings)."""
    errs = []
    if decl.audience not in host.serves:
        errs.append(f"host {host.name} serves {host.serves}, not audience {decl.audience!r}")
    anon = str(settings.get("auth.anonymous", {}).get("enabled", "false")).lower() == "true"
    if decl.sensitivity == "sensitive" and anon:
        errs.append(f"host {host.name} lets anonymous visitors in: sensitive boards cannot "
                    "live there (and anonymous viewers can query every datasource)")
    return errs


_SCRIPTY = re.compile(r"<\s*script|\bon[a-z]+\s*=|javascript:", re.I)


def board_errors(board: dict) -> list[str]:
    """Refuse text panels that carry script. A host may run with
    GF_PANELS_DISABLE_SANITIZE_HTML=true (an iframe embed needs it), and then a
    text panel's HTML runs in every viewer's session, an admin's included."""
    errs = []

    def walk(panels):
        for p in panels:
            if p.get("type") == "text":
                content = (p.get("options") or {}).get("content") or p.get("content") or ""
                if _SCRIPTY.search(content):
                    errs.append(f"text panel {p.get('id')} {p.get('title')!r} carries script "
                                "(<script>, an on*= handler or javascript:): refused")
            walk(p.get("panels", []))
    walk(board.get("panels", []))
    return errs
