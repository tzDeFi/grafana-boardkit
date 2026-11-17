"""Deploy = bundle the repo's boards, ship them to the host, apply them THERE.

    repo:  boardkit deploy boards.toml
             -> bundle: declaration.json + boards/<uid>.json + this library's source
             -> host is this machine?  apply in-process
                else rsync to <ssh>:<store>/projects/<project>/ and
                     ssh <ssh> python3 -m boardkit apply --project <project>

Deploy publishes boards, folders, orgs and datasources. It never touches who
may open them: that is Grafana's (grafana.AccessIsGrafanas refuses the writes).
The host keeps the last bundle of every project, so a re-apply needs nothing
from the repo. Admin credentials stay on the host.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from . import __version__
from .datasources import rewire
from .decl import Declaration, from_dict, load, placements
from .grafana import GrafanaError
from .hosts import Host, Inventory, load_inventory
from .orgs import MAIN_ORG_ID, ensure_datasource, ensure_org
from .policy import board_errors, host_errors
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
    return from_dict(d, path, legacy=True)


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


def apply_project(host: Host, decl: Declaration, *, reset_vars: bool = False, g=None) -> dict:
    """On the host: publish the project's boards. Access is not touched."""
    g = g or host.grafana()
    report = {"project": decl.project, "host": host.name, "published": [],
              "changed": [], "problems": [], "warnings": []}
    errs = host_errors(decl, host, g.settings())
    if errs:
        report["problems"] = errs
        return report                      # refuse before touching anything
    env = host.admin_env()
    org_ids = {None: MAIN_ORG_ID}
    for name in sorted(decl.owned_orgs()):
        org_ids[name] = ensure_org(g, name, report["changed"])
    for org, f, b, isolated in placements(decl):
        go = g.in_org(org_ids[org])
        report["changed"] += ensure_folder(go, f.uid, f.title)
        targets = _datasource_targets(go, host, decl.datasource_map(f, b, isolated), org,
                                      env, report["changed"])
        raw = json.loads(b.file.read_text())
        bad = board_errors(raw)
        if bad:
            report["problems"] += [f"{b.uid}: {e}" for e in bad]
            continue
        try:
            board = rewire(raw, targets)
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
    return report


class _HostLock:
    """One apply at a time on a host: two deploys never race to publish the
    same board."""

    def __init__(self, path: Path, wait_s: float = 180):
        self.path, self.wait_s, self.f = path, wait_s, None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.f = open(self.path, "w")
        t0 = time.time()
        while True:
            try:
                fcntl.flock(self.f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except BlockingIOError:
                if time.time() - t0 > self.wait_s:
                    raise SystemExit(f"another boardkit apply holds {self.path}") from None
                time.sleep(1)

    def __exit__(self, *exc):
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


def apply_stored(host: Host, project: str | None, **kw) -> list[dict]:
    """Publish stored bundles, each on its own: one project's failure is a
    problem in its report, never a reason to stop publishing the others."""
    root = host.store_path / "projects"
    with _HostLock(host.store_path / "apply.lock"):
        all_names = sorted(p.name for p in root.iterdir() if p.is_dir() and
                           (p / "declaration.json").is_file())
        decls, reports = {}, []
        for n in all_names:
            try:
                decls[n] = load_bundle(root / n)
            except Exception as e:  # noqa: BLE001
                reports.append({"project": n, "host": host.name, "published": [], "changed": [],
                                "warnings": [], "problems": [f"stored bundle unreadable: {e}"]})
        for n in ([project] if project else sorted(decls)):
            try:
                reports.append(apply_project(host, decls[n], **kw))
            except Exception as e:  # noqa: BLE001  (a crash here must not stall the rest)
                reports.append({"project": n, "host": host.name, "published": [],
                                "changed": [], "warnings": [],
                                "problems": [f"apply failed: {type(e).__name__}: {e}"]})
        return reports


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, capture_output=True, text=True)


def _rsync_path(store: str) -> str:
    """rsync resolves a relative remote path against the login directory; a
    leading ~ is not reliably expanded across rsync versions."""
    return store.removeprefix("~/")


def _version(text: str) -> tuple[int, ...]:
    m = re.search(r'__version__ = "([0-9.]+)"', text)
    return tuple(int(x) for x in m.group(1).split(".")) if m else (0,)


def ship(host: Host, bundle_dir: Path, project: str, run=_run) -> None:
    """rsync the bundle and the library onto the host, atomically enough:
    each lands in a temp dir that is swapped in with one mv. The host keeps ONE
    library for every stored project and the timer, so an older one never
    replaces a newer one (set BOARDKIT_ALLOW_DOWNGRADE=1 to force)."""
    store = host.store.replace("~", "$HOME", 1)
    have = run(["ssh", host.ssh,
                f"cat {store}/lib/boardkit/__init__.py 2>/dev/null || true"]).stdout
    mine = (LIB_DIR / "__init__.py").read_text()
    if _version(have) > _version(mine) and os.environ.get("BOARDKIT_ALLOW_DOWNGRADE") != "1":
        raise SystemExit(f"host {host.name} runs boardkit {'.'.join(map(str, _version(have)))}, "
                         f"newer than this {'.'.join(map(str, _version(mine)))}: upgrade the "
                         "repo's pin rather than downgrading every project on the host")
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


def remote_apply(host: Host, project: str, *, reset_vars: bool, run=_run) -> list[dict]:
    store = host.store.replace("~", "$HOME", 1)
    args = ["apply", "--project", project, "--json"]
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


def deploy(decl_path, *, inventory: Inventory | None = None, reset_vars=False,
           run=_run) -> list[dict]:
    decl = load(decl_path)
    inv = inventory or load_inventory()
    host = inv.for_audience(decl.audience)
    with tempfile.TemporaryDirectory(prefix="boardkit-") as tmp:
        b = bundle(decl, Path(tmp) / decl.project)
        if inv.is_self(host):
            dest = host.store_path / "projects" / decl.project
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(b, dest)
            return apply_stored(host, decl.project, reset_vars=reset_vars)
        ship(host, b, decl.project, run=run)
    return remote_apply(host, decl.project, reset_vars=reset_vars, run=run)
