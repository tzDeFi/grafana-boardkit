"""Deploy = bundle the repo's boards, ship them to the host, apply them THERE.

    repo:  boardkit deploy boards.toml
             -> bundle: declaration.json + boards/<uid>.json + this library's source
             -> host is this machine?  apply in-process
                else rsync to <ssh>:<store>/projects/<project>/ and
                     ssh <ssh> python3 -m boardkit apply --project <project>
    host:  boardkit apply --all --access-only   (timer) seats accounts that
           registered since the last deploy

The host keeps the last bundle of every project, so the timer and a re-apply
need nothing from the repo. Admin credentials stay on the host.
"""
from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import __version__
from .access import apply_access, placement_viewers
from .datasources import references, rewire
from .decl import Declaration, from_dict, load
from .grafana import GrafanaError
from .hosts import Host, Inventory, load_inventory
from .orgs import MAIN_ORG_ID, ensure_datasource, ensure_org, org_members
from .policy import host_errors, static_errors
from .publish import ensure_folder, publish_board

LIB_DIR = Path(__file__).resolve().parent


def bundle(decl: Declaration, out: Path) -> Path:
    """Self-contained copy of what to deploy: the host needs nothing else."""
    (out / "boards").mkdir(parents=True, exist_ok=True)
    for _, b in decl.boards():
        shutil.copyfile(b.file, out / "boards" / f"{b.uid}.json")
    d = decl.to_dict(board_path=lambda b: f"boards/{b.uid}.json")
    d["boardkit_version"] = __version__
    (out / "declaration.json").write_text(json.dumps(d, indent=1, ensure_ascii=False))
    return out


def load_bundle(path: Path) -> Declaration:
    d = json.loads((path / "declaration.json").read_text())
    d.pop("boardkit_version", None)
    return from_dict(d, path)


def _datasource_targets(g, host: Host, mapping: dict[str, str], org: str | None,
                        env: dict, log: list[str]) -> dict[str, dict]:
    """board reference -> {"type", "uid"} in the org `g` acts in. In the main
    org a logical name may be an existing (provisioned) uid; anywhere else it
    must have a definition, and boardkit creates the datasource there."""
    existing = {d["uid"]: d for d in g("/api/datasources")}
    out = {}
    for ref, logical in mapping.items():
        uid = host.datasources.get(logical) if org is None else None
        if uid is not None:
            if uid not in existing:
                raise SystemExit(f"host {host.name}: datasource uid {uid!r} does not exist")
            out[ref] = {"type": existing[uid]["type"], "uid": uid}
        elif logical in host.datasource_defs:
            out[ref] = ensure_datasource(g, logical, host.datasource_defs[logical], env, log,
                                         org or "main")
        else:
            raise SystemExit(f"host {host.name} has no datasource for logical name {logical!r}"
                             f" in org {org or 'main'!r} (add [hosts.{host.name}."
                             f"datasource_defs.{logical}])")
    return out


def _exposure(g, decl: Declaration, org_ids: dict) -> list[str]:
    """Per org: accounts that can query the data behind a board they cannot
    open (any org member can POST SQL to any datasource of the org)."""
    if decl.sensitivity != "sensitive":
        return []
    boards: dict[str | None, list] = {}
    for org, f, b, isolated, who in placement_viewers(decl):
        refs = references(json.loads(b.file.read_text()))
        m = decl.datasource_map(f, b, isolated)
        boards.setdefault(org, []).append((b.uid, who, {m[r] for r in refs if r in m}))
    out = []
    for org, items in boards.items():
        used = set().union(*(ds for _, _, ds in items))
        if not used:
            continue
        members = {e for e, u in org_members(g, org_ids[org]).items()
                   if u["role"] != "Admin" and u.get("login") != "admin"}
        exposed = sorted(e for e in members
                         if any(ds and e not in who for _, who, ds in items))
        if exposed:
            out.append(f"{len(exposed)} account(s) in org {org or 'main'!r} can query the data "
                       f"behind boards they cannot open (datasources {sorted(used)}; Grafana "
                       "OSS lets any org member send SQL). Isolate the audience: its own org "
                       "and a row-restricted database role.")
    return out


def apply_project(host: Host, decl: Declaration, *, access_only: bool = False,
                  reset_vars: bool = False, g=None, others=()) -> dict:
    """On the host: publish (unless access_only) and converge access."""
    g = g or host.grafana()
    report = {"project": decl.project, "host": host.name, "published": [],
              "changed": [], "invites": {}, "problems": [], "warnings": []}
    errs = static_errors(decl) + host_errors(decl, host, g.settings())
    if errs:
        report["problems"] = errs
        return report                      # refuse before touching anything
    env = host.admin_env()
    org_ids = {None: MAIN_ORG_ID}
    for name in sorted(decl.owned_orgs()):
        org_ids[name] = ensure_org(g, name, report["changed"])
    if not access_only:
        for org, f, b, isolated, _who in placement_viewers(decl):
            go = g.in_org(org_ids[org])
            report["changed"] += ensure_folder(go, f.uid, f.title)
            targets = _datasource_targets(go, host, decl.datasource_map(f, b, isolated), org,
                                          env, report["changed"])
            try:
                board = rewire(json.loads(b.file.read_text()), targets)
            except ValueError as e:
                report["problems"].append(str(e))
                continue
            tabs = f.tabs if b.tabs is None else b.tabs
            try:
                r = publish_board(go, board, f.uid, tabs=tabs, reset_vars=reset_vars,
                                  preference_vars=b.preference_vars,
                                  message=f"boardkit {__version__} ({decl.project})")
            except GrafanaError as e:
                report["problems"].append(f"{b.uid}{' in ' + org if org else ''}: {e}")
                continue
            r["org"] = org
            report["published"].append(r)
            report["problems"] += [f"{b.uid}: {p}" for p in r["problems"]]
    acc = apply_access(g, decl, host.root_url, org_ids, others)
    for k in ("changed", "problems", "warnings"):
        report[k] += acc[k]
    report["invites"] = acc["invites"]
    report["warnings"] += _exposure(g, decl, org_ids)
    return report


def apply_stored(host: Host, project: str | None, **kw) -> list[dict]:
    root = host.store_path / "projects"
    all_names = sorted(p.name for p in root.iterdir() if p.is_dir() and
                       (p / "declaration.json").is_file())
    decls = {n: load_bundle(root / n) for n in all_names}
    names = [project] if project else all_names
    return [apply_project(host, decls[n], others=[d for m, d in decls.items() if m != n], **kw)
            for n in names]


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, capture_output=True, text=True)


def _rsync_path(store: str) -> str:
    """rsync resolves a relative remote path against the login directory; a
    leading ~ is not reliably expanded across rsync versions."""
    return store.removeprefix("~/")


def ship(host: Host, bundle_dir: Path, project: str, run=_run) -> None:
    """rsync the bundle and the library onto the host, atomically enough:
    each lands in a temp dir that is swapped in with one mv."""
    store = host.store.replace("~", "$HOME", 1)
    rs = _rsync_path(host.store)
    remote = (f"set -e; mkdir -p {store}/projects {store}/tmp; "
              f"rm -rf {store}/tmp/{project} {store}/tmp/lib; mkdir -p {store}/tmp/lib")
    run(["ssh", host.ssh, remote])
    run(["rsync", "-a", "--delete", f"{bundle_dir}/", f"{host.ssh}:{rs}/tmp/{project}/"])
    run(["rsync", "-a", "--delete", "--exclude", "__pycache__", f"{LIB_DIR}/",
         f"{host.ssh}:{rs}/tmp/lib/boardkit/"])
    swap = (f"set -e; cd {store}; rm -rf projects/{project}.old lib.old; "
            f"[ -d projects/{project} ] && mv projects/{project} projects/{project}.old; "
            f"mv tmp/{project} projects/{project}; "
            f"[ -d lib ] && mv lib lib.old; mv tmp/lib lib; "
            f"rm -rf projects/{project}.old lib.old")
    run(["ssh", host.ssh, swap])


def remote_apply(host: Host, project: str, *, access_only: bool, reset_vars: bool,
                 run=_run) -> list[dict]:
    store = host.store.replace("~", "$HOME", 1)
    args = ["apply", "--project", project, "--json"]
    if access_only:
        args.append("--access-only")
    if reset_vars:
        args.append("--reset-vars")
    cmd = f"PYTHONPATH={store}/lib python3 -m boardkit " + " ".join(map(shlex.quote, args))
    try:
        out = run(["ssh", host.ssh, cmd]).stdout
    except subprocess.CalledProcessError as e:
        if e.stdout.strip().startswith("["):
            return json.loads(e.stdout)    # problems reported: exit 1 with a report
        raise SystemExit(f"remote apply on {host.name} failed:\n{e.stderr[-2000:]}") from None
    return json.loads(out)


def deploy(decl_path, *, inventory: Inventory | None = None, access_only=False,
           reset_vars=False, run=_run) -> list[dict]:
    decl = load(decl_path)
    errs = static_errors(decl)
    if errs:
        raise SystemExit("; ".join(errs))
    inv = inventory or load_inventory()
    host = inv.for_audience(decl.audience)
    with tempfile.TemporaryDirectory(prefix="boardkit-") as tmp:
        b = bundle(decl, Path(tmp) / decl.project)
        if inv.is_self(host):
            dest = host.store_path / "projects" / decl.project
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(b, dest)
            return apply_stored(host, decl.project, access_only=access_only,
                                reset_vars=reset_vars)
        ship(host, b, decl.project, run=run)
    return remote_apply(host, decl.project, access_only=access_only, reset_vars=reset_vars,
                        run=run)
