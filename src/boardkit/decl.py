"""The declaration a repo keeps next to its boards: what to deploy, to whom.

    project     = "risk"            # names the repo's teams and its store on the host
    audience    = "internal"        # internal -> the team host; me -> David's own host
    sensitivity = "sensitive"       # sensitive | open

    [datasources]                   # reference in the board JSON -> logical name
    "${DS_MAIN}" = "main"           # (the host maps logical names to its own uids)

    [[folders]]
    uid     = "risk"
    title   = "Risk"
    viewers = ["a@x.com"]           # folder-wide; "all" = every account (open only)
    team    = "Risk desk"           # optional: keep an existing team's name
    tabs    = true                  # publish boards with rows as real tabs

      [[folders.boards]]
      file    = "grafana/book-a.json"
      viewers = ["m@y.com"]         # this board only
      team    = "Client A"          # optional
      home    = true                # the board is those viewers' home page
      preference_vars = ["loss"]    # carried over even under --reset-vars

Orgs (a sensitive project's real boundary: Grafana OSS lets any org member
query any of that org's datasources, so only an org keeps a viewer out of
data they may not see):

    [[folders]]
    org = "Risk"                    # the folder lives in its own org; its
    [folders.datasources]           # datasources are created there from the
    "${DS_MAIN}" = "risk_all"       # host inventory's [datasource_defs]
      [[folders.boards]]
      viewers = ["m@y.com"]
      org     = "Risk · client-a"   # ISOLATED: also published into this org,
      [folders.boards.datasources]  # where its viewers are the only members,
      "${DS_MAIN}" = "risk_client_a" # through a datasource that sees their rows

A project owns every org it names: members are exactly the declared viewers.
Someone who is a viewer only inside such orgs is taken out of the main org.

TOML or JSON. Unknown keys are rejected: a typo must not silently drop a
restriction. Paths are relative to the declaration file.
"""
from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

AUDIENCES = ("me", "internal")
SENSITIVITIES = ("sensitive", "open")
ALL = "all"
MAIN_ORG_NAMES = ("Main Org.", "Main Org")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class DeclarationError(ValueError):
    pass


@dataclass
class Board:
    file: Path
    uid: str
    title: str
    viewers: list[str] = field(default_factory=list)
    team: str | None = None
    home: bool = False
    preference_vars: list[str] = field(default_factory=list)
    tabs: bool | None = None
    org: str | None = None                      # isolated into its own org
    datasources: dict[str, str] = field(default_factory=dict)


@dataclass
class Folder:
    uid: str
    title: str
    viewers: list[str] | str = field(default_factory=list)   # list or ALL
    team: str | None = None
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

    # ── derived names ─────────────────────────────────────────────────────
    def team_prefix(self) -> str:
        return f"{self.project} · "

    def folder_team(self, f: Folder) -> str | None:
        if f.viewers == ALL:
            return None
        return f.team or f"{self.team_prefix()}{f.title}"

    def board_team(self, b: Board) -> str | None:
        if not b.viewers:
            return None
        return b.team or f"{self.team_prefix()}{b.uid}"

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
                "uid": f.uid, "title": f.title, "viewers": f.viewers, "team": f.team,
                "tabs": f.tabs, "org": f.org, "datasources": dict(f.datasources),
                "boards": [{"file": board_path(b), "viewers": b.viewers, "team": b.team,
                            "home": b.home, "preference_vars": b.preference_vars,
                            "tabs": b.tabs, "org": b.org, "datasources": dict(b.datasources)}
                           for b in f.boards]}
                for f in self.folders]}


def _keys(d: dict, allowed: set, where: str) -> None:
    extra = set(d) - allowed
    if extra:
        raise DeclarationError(f"{where}: unknown keys {sorted(extra)}")


def _emails(v, where: str, allow_all: bool) -> list[str] | str:
    if v == ALL and allow_all:
        return ALL
    if not isinstance(v, list):
        raise DeclarationError(f"{where}: viewers must be a list of emails"
                               + (' or "all"' if allow_all else ""))
    out = []
    for e in v:
        if not isinstance(e, str) or not _EMAIL.match(e.strip()):
            raise DeclarationError(f"{where}: {e!r} is not an email")
        out.append(e.strip().lower())
    if len(set(out)) != len(out):
        raise DeclarationError(f"{where}: duplicate viewers")
    return sorted(out)


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


def from_dict(d: dict, root: Path) -> Declaration:
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
        _keys(fd, {"uid", "title", "viewers", "team", "tabs", "boards", "org", "datasources"},
              where)
        if not _SLUG.match(fd.get("uid", "")):
            raise DeclarationError(f"{where}: uid must be a slug (<= 40 chars)")
        f = Folder(uid=fd["uid"], title=fd.get("title") or fd["uid"],
                   viewers=_emails(fd.get("viewers", []), where, allow_all=True),
                   team=fd.get("team"), tabs=bool(fd.get("tabs", False)),
                   org=_org(fd.get("org"), where), datasources=_dsmap(fd, where))
        for j, bd in enumerate(fd.get("boards", [])):
            bwhere = f"{where}.boards[{j}]"
            _keys(bd, {"file", "viewers", "team", "home", "preference_vars", "tabs", "org",
                       "datasources"}, bwhere)
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
                      viewers=_emails(bd.get("viewers", []), bwhere, allow_all=False),
                      team=bd.get("team"), home=bool(bd.get("home", False)),
                      preference_vars=list(bd.get("preference_vars", [])),
                      tabs=bd.get("tabs"), org=_org(bd.get("org"), bwhere),
                      datasources=_dsmap(bd, bwhere))
            if b.home and not b.viewers:
                raise DeclarationError(f"{bwhere}: home = true needs board viewers")
            if b.org and not b.viewers:
                raise DeclarationError(f"{bwhere}: an isolated board (org) needs viewers")
            if b.org and b.org == f.org:
                raise DeclarationError(f"{bwhere}: org {b.org!r} is the folder's own org")
            if b.datasources and not b.org:
                raise DeclarationError(f"{bwhere}: board datasources apply only to an "
                                       "isolated board (set org)")
            f.boards.append(b)
        folders.append(f)
    if not folders:
        raise DeclarationError("a declaration needs at least one [[folders]]")
    decl = Declaration(project=project, audience=d["audience"], sensitivity=d["sensitivity"],
                       datasources=dict(ds), folders=folders, root=root)
    teams = [decl.folder_team(f) for f in folders] + [decl.board_team(b) for _, b in decl.boards()]
    teams = [t for t in teams if t]
    if len(set(teams)) != len(teams):
        raise DeclarationError(f"two groups share a team name: {teams}")
    return decl


def load(path: str | Path) -> Declaration:
    path = Path(path).resolve()
    raw = path.read_bytes()
    d = tomllib.loads(raw.decode()) if path.suffix == ".toml" else json.loads(raw)
    return from_dict(d, path.parent)
