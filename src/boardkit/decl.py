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


@dataclass
class Folder:
    uid: str
    title: str
    viewers: list[str] | str = field(default_factory=list)   # list or ALL
    team: str | None = None
    tabs: bool = False
    boards: list[Board] = field(default_factory=list)


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

    def to_dict(self, board_path=lambda b: str(b.file)) -> dict:
        return {
            "project": self.project, "audience": self.audience,
            "sensitivity": self.sensitivity, "datasources": dict(self.datasources),
            "folders": [{
                "uid": f.uid, "title": f.title, "viewers": f.viewers, "team": f.team,
                "tabs": f.tabs,
                "boards": [{"file": board_path(b), "viewers": b.viewers, "team": b.team,
                            "home": b.home, "preference_vars": b.preference_vars,
                            "tabs": b.tabs} for b in f.boards]}
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
        _keys(fd, {"uid", "title", "viewers", "team", "tabs", "boards"}, where)
        if not _SLUG.match(fd.get("uid", "")):
            raise DeclarationError(f"{where}: uid must be a slug (<= 40 chars)")
        f = Folder(uid=fd["uid"], title=fd.get("title") or fd["uid"],
                   viewers=_emails(fd.get("viewers", []), where, allow_all=True),
                   team=fd.get("team"), tabs=bool(fd.get("tabs", False)))
        for j, bd in enumerate(fd.get("boards", [])):
            bwhere = f"{where}.boards[{j}]"
            _keys(bd, {"file", "viewers", "team", "home", "preference_vars", "tabs"}, bwhere)
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
                      tabs=bd.get("tabs"))
            if b.home and not b.viewers:
                raise DeclarationError(f"{bwhere}: home = true needs board viewers")
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
