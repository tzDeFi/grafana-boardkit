"""What audience and sensitivity mean, as checks.

Audience picks the host: a board for David alone lives on his own machine, a
board for anyone else on the team host. Sensitivity picks the rules:

  sensitive   the host must require login (no anonymous role at all);
              viewers are explicit emails, never "all";
              no builtin role (Viewer / Editor) may hold a grant on its folders.
  open        "all" is allowed: every account on the host may view.

One risk no permission can close on Grafana OSS: any org member can POST
/api/ds/query with hand-written SQL to any datasource in the org. For a
sensitive project, every apply counts, per org, the accounts that can query
the data behind a board they cannot open (deploy._exposure). The fix is an org
per audience (datasources are per org) with a database role that sees only
that audience's rows: `org` on a folder or board in the declaration.
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
