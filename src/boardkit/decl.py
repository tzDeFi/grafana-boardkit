"""The declaration a repo keeps next to its boards: what to deploy, and where.

    project     = "risk"            # names the repo's store on the host
    audience    = "internal"        # internal -> the team host; me -> the owner's own host
    sensitivity = "sensitive"       # sensitive | open

    [datasources]                   # reference in the board JSON -> logical name
    "${DS_MAIN}" = "main"           # (the host maps logical names to its own uids)

    [[folders]]
    uid     = "risk"
    title   = "Risk"
    tabs    = true                  # publish boards with rows as real tabs

      [[folders.boards]]
      file    = "grafana/book-a.json"
      preference_vars = ["loss"]    # carried over even under --reset-vars

Who may open a board is NOT declared here. It is set in Grafana (Administration,
Users and access), and boardkit never reads or changes it: no users, teams,
org members, folder or board permissions, home pages. `viewers`, `team` and
`home` are refused, so a repo cannot grow a second access system.

Orgs (a sensitive project's real boundary: Grafana OSS lets any org member
query any of that org's datasources, so only an org keeps a person out of data
they may not see). boardkit creates the org and the datasources in it; who is a
member of it is Grafana's:

    [[folders]]
    org = "Risk"                    # the folder lives in its own org; its
    [folders.datasources]           # datasources are created there from the
    "${DS_MAIN}" = "risk_all"       # host inventory's [datasource_defs]
      [[folders.boards]]
      org     = "Risk · client-a"   # ISOLATED: also published into this org,
      [folders.boards.datasources]  # through a datasource that sees fewer rows
      "${DS_MAIN}" = "risk_client_a"

TOML or JSON. Unknown keys are rejected: a typo must not silently drop a
setting. Paths are relative to the declaration file.
"""
from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

AUDIENCES = ("me", "internal")
SENSITIVITIES = ("sensitive", "open")
MAIN_ORG_NAMES = ("Main Org.", "Main Org")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")

#: keys boardkit <= 0.3 used to put access in a declaration. Refused in a repo's
#: declaration; ignored in a bundle an older boardkit stored on a host.
RETIRED_KEYS = ("viewers", "team", "home")


class DeclarationError(ValueError):
    pass


@dataclass
class Board:
    file: Path
    uid: str
    title: str
    preference_vars: list[str] = field(default_factory=list)
    tabs: bool | None = None
    org: str | None = None                      # also published into its own org
    datasources: dict[str, str] = field(default_factory=dict)


@dataclass
class Folder:
    uid: str
    title: str
    tabs: bool = False
    boards: list[Board] = field(default_factory=list)
    org: str | None = None                      # None = the main org
    datasources: dict[str, str] = field(default_factory=dict)


@dataclass
class Declaration:
    project: str
    audience: str
    sensitivity: str
    datasources: dict[str, str]
    folders: list[Folder]
    root: Path

    def boards(self):
        for f in self.folders:
            for b in f.boards:
                yield f, b

    def datasource_map(self, f: Folder, b: Board | None = None, isolated: bool = False) -> dict:
        m = {**self.datasources, **f.datasources}
        if isolated and b is not None:
            m.update(b.datasources)
        return m

    def owned_orgs(self) -> set[str]:
        return {f.org for f in self.folders if f.org} | {b.org for _, b in self.boards() if b.org}

    def to_dict(self, board_path=lambda b: str(b.file)) -> dict:
        return {
            "project": self.project, "audience": self.audience,
            "sensitivity": self.sensitivity, "datasources": dict(self.datasources),
            "folders": [{
                "uid": f.uid, "title": f.title, "tabs": f.tabs, "org": f.org,
                "datasources": dict(f.datasources),
                "boards": [{"file": board_path(b), "preference_vars": b.preference_vars,
                            "tabs": b.tabs, "org": b.org, "datasources": dict(b.datasources)}
                           for b in f.boards]}
                for f in self.folders]}


def placements(decl: Declaration):
    """(org name | None, folder, board, isolated): every place a board is published."""
    for f in decl.folders:
        for b in f.boards:
            yield f.org, f, b, False
            if b.org:
                yield b.org, f, b, True


def _keys(d: dict, allowed: set, where: str, legacy: bool = False) -> None:
    retired = sorted(set(d) & set(RETIRED_KEYS))
    if retired and not legacy:
        raise DeclarationError(
            f"{where}: {retired} is retired (boardkit 0.4): who can open a board is set in "
            "Grafana (Administration, Users and access), never in a declaration. Remove it.")
    extra = set(d) - allowed - (set(RETIRED_KEYS) if legacy else set())
    if extra:
        raise DeclarationError(f"{where}: unknown keys {sorted(extra)}")


def _org(v, where: str) -> str | None:
    if v is None:
        return None
    if not isinstance(v, str) or not v.strip() or v.strip() in MAIN_ORG_NAMES:
        raise DeclarationError(f"{where}: org must be a name other than the main org")
    return v.strip()


def _dsmap(d: dict, where: str) -> dict[str, str]:
    m = d.get("datasources", {})
    if not isinstance(m, dict) or not all(isinstance(v, str) for v in m.values()):
        raise DeclarationError(f"{where}: datasources maps board references to logical names")
    return dict(m)


def from_dict(d: dict, root: Path, *, legacy: bool = False) -> Declaration:
    """`legacy`: a bundle stored by boardkit <= 0.3 on a host, which still carries
    the retired access keys. They are ignored, never acted on."""
    _keys(d, {"project", "audience", "sensitivity", "datasources", "folders"}, "declaration")
    project = d.get("project", "")
    if not _SLUG.match(project):
        raise DeclarationError(f"project {project!r}: lowercase letters, digits and -")
    if d.get("audience") not in AUDIENCES:
        raise DeclarationError(f"audience must be one of {AUDIENCES}")
    if d.get("sensitivity") not in SENSITIVITIES:
        raise DeclarationError(f"sensitivity must be one of {SENSITIVITIES}")
    ds = d.get("datasources", {})
    if not isinstance(ds, dict) or not all(isinstance(v, str) for v in ds.values()):
        raise DeclarationError("[datasources] maps board references to logical names")
    folders, uids = [], set()
    for i, fd in enumerate(d.get("folders", [])):
        where = f"folders[{i}]"
        _keys(fd, {"uid", "title", "tabs", "boards", "org", "datasources"}, where, legacy)
        if not _SLUG.match(fd.get("uid", "")):
            raise DeclarationError(f"{where}: uid must be a slug (<= 40 chars)")
        f = Folder(uid=fd["uid"], title=fd.get("title") or fd["uid"],
                   tabs=bool(fd.get("tabs", False)),
                   org=_org(fd.get("org"), where), datasources=_dsmap(fd, where))
        for j, bd in enumerate(fd.get("boards", [])):
            bwhere = f"{where}.boards[{j}]"
            _keys(bd, {"file", "preference_vars", "tabs", "org", "datasources"}, bwhere, legacy)
            path = (root / bd["file"]).resolve()
            if not path.is_file():
                raise DeclarationError(f"{bwhere}: no board file {bd['file']}")
            board = json.loads(path.read_text())
            uid = board.get("uid")
            if not uid:
                raise DeclarationError(f"{bwhere}: {bd['file']} has no uid")
            if uid in uids:
                raise DeclarationError(f"{bwhere}: board uid {uid} declared twice")
            uids.add(uid)
            b = Board(file=path, uid=uid, title=board.get("title", uid),
                      preference_vars=list(bd.get("preference_vars", [])),
                      tabs=bd.get("tabs"), org=_org(bd.get("org"), bwhere),
                      datasources=_dsmap(bd, bwhere))
            if b.org and b.org == f.org:
                raise DeclarationError(f"{bwhere}: org {b.org!r} is the folder's own org")
            if b.datasources and not b.org:
                raise DeclarationError(f"{bwhere}: board datasources apply only to an "
                                       "isolated board (set org)")
            f.boards.append(b)
        folders.append(f)
    if not folders:
        raise DeclarationError("a declaration needs at least one [[folders]]")
    return Declaration(project=project, audience=d["audience"], sensitivity=d["sensitivity"],
                       datasources=dict(ds), folders=folders, root=root)


def load(path: str | Path) -> Declaration:
    path = Path(path).resolve()
    raw = path.read_bytes()
    d = tomllib.loads(raw.decode()) if path.suffix == ".toml" else json.loads(raw)
    return from_dict(d, path.parent)
