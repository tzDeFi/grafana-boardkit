"""What audience and sensitivity mean, as checks.

Audience picks the host: a board for David alone lives on his own machine, a
board for anyone else on the team host. Sensitivity picks the rules:

  sensitive   the host must require login (no anonymous role at all);
              viewers are explicit emails, never "all";
              no builtin role (Viewer / Editor) may hold a grant on its folders.
  open        "all" is allowed: every account on the host may view.

One risk no permission can close on Grafana OSS: any org member can POST
/api/ds/query with hand-written SQL to any datasource in the org. For a
sensitive project whose datasource other people's accounts can reach, that is
reported as a warning with the head count; the fix is a separate Grafana org
(datasources are per org) or a database role that only sees that audience's
rows.
"""
from __future__ import annotations

from .decl import ALL, Declaration


def static_errors(decl: Declaration) -> list[str]:
    errs = []
    if decl.sensitivity == "sensitive":
        for f in decl.folders:
            if f.viewers == ALL:
                errs.append(f"folder {f.uid}: a sensitive project cannot grant \"all\"")
    return errs


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


def datasource_warnings(decl: Declaration, org_emails: set[str], viewers: set[str],
                        logical: set[str]) -> list[str]:
    """Who can read a sensitive project's data WITHOUT being allowed its boards.

    Two groups, both real on Grafana OSS: accounts outside the project, and a
    board's own viewers, who can query the rows behind every OTHER board on the
    same datasource (one client reading another's book)."""
    if decl.sensitivity != "sensitive" or not logical:
        return []
    out = []
    outside = sorted(org_emails - viewers)
    if outside:
        out.append(f"{len(outside)} account(s) outside this project's viewers can query its "
                   f"datasource(s) {sorted(logical)} with hand-written SQL (Grafana OSS). "
                   "Isolate with a per-audience org or a row-restricted database role.")
    scoped = [b for _, b in decl.boards() if b.viewers]
    if scoped and len(list(decl.boards())) > 1:
        out.append(f"{len(scoped)} board-only viewer group(s) share datasource(s) "
                   f"{sorted(logical)} with the project's other boards: each can query the "
                   "rows behind boards it cannot open. Give each audience its own org with a "
                   "database role that sees only its rows.")
    return out
