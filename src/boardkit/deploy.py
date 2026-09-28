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
from .access import apply_access
from .datasources import references, rewire
from .decl import Declaration, from_dict, load
from .grafana import GrafanaError
from .hosts import Host, Inventory, load_inventory
from .policy import datasource_warnings, host_errors, static_errors
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


def _datasource_targets(g, host: Host, decl: Declaration) -> dict[str, dict]:
    by_uid = {d["uid"]: d for d in g("/api/datasources")}
    out = {}
    for ref, logical in decl.datasources.items():
        uid = host.datasources.get(logical)
        if uid is None:
            raise SystemExit(f"host {host.name} has no datasource for logical name "
                             f"{logical!r} (add it to [hosts.{host.name}.datasources])")
        if uid not in by_uid:
            raise SystemExit(f"host {host.name}: datasource uid {uid!r} does not exist")
        out[ref] = {"type": by_uid[uid]["type"], "uid": uid}
    return out


def apply_project(host: Host, decl: Declaration, *, access_only: bool = False,
                  reset_vars: bool = False, g=None) -> dict:
    """On the host: publish (unless access_only) and converge access."""
    g = g or host.grafana()
    report = {"project": decl.project, "host": host.name, "published": [],
              "changed": [], "invites": {}, "problems": [], "warnings": []}
    errs = static_errors(decl) + host_errors(decl, host, g.settings())
    if errs:
        report["problems"] = errs
        return report                      # refuse before touching anything
    if not access_only:
        targets = _datasource_targets(g, host, decl)
        for f in decl.folders:
            report["changed"] += ensure_folder(g, f.uid, f.title)
            for b in f.boards:
                board = rewire(json.loads(b.file.read_text()), targets)
                tabs = f.tabs if b.tabs is None else b.tabs
                try:
                    r = publish_board(g, board, f.uid, tabs=tabs, reset_vars=reset_vars,
                                      preference_vars=b.preference_vars,
                                      message=f"boardkit {__version__} ({decl.project})")
                except GrafanaError as e:
                    report["problems"].append(f"{b.uid}: {e}")
                    continue
                report["published"].append(r)
                report["problems"] += [f"{b.uid}: {p}" for p in r["problems"]]
    acc = apply_access(g, decl, host.root_url)
    for k in ("changed", "problems", "warnings"):
        report[k] += acc[k]
    report["invites"] = acc["invites"]
    viewers = set()
    for f in decl.folders:
        if isinstance(f.viewers, list):
            viewers |= set(f.viewers)
        for b in f.boards:
            viewers |= set(b.viewers)
    org = {u["email"].lower() for u in g("/api/org/users") if u.get("login") != "admin"}
    used = {decl.datasources[r] for _, b in decl.boards()
            for r in references(json.loads(b.file.read_text())) if r in decl.datasources}
    report["warnings"] += datasource_warnings(decl, org, viewers, used)
    return report


def apply_stored(host: Host, project: str | None, **kw) -> list[dict]:
    root = host.store_path / "projects"
    names = [project] if project else sorted(p.name for p in root.iterdir() if p.is_dir())
    return [apply_project(host, load_bundle(root / n), **kw) for n in names]


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
            return [apply_project(host, load_bundle(dest), access_only=access_only,
                                  reset_vars=reset_vars)]
        ship(host, b, decl.project, run=run)
    return remote_apply(host, decl.project, access_only=access_only, reset_vars=reset_vars,
                        run=run)
