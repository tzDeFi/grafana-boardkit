"""The host inventory: the private half of boardkit.

It is a file on each machine, never in a repo (it names machines and where
their admin credentials live), at $BOARDKIT_HOSTS or
~/.config/boardkit/hosts.toml:

    self = "myhost"                        # which entry is THIS machine

    [hosts.teamhost]
    serves      = ["internal"]             # audiences it hosts
    ssh         = "teamhost"               # how other machines reach it
    grafana_url = "http://localhost:3000"  # as seen on the host itself
    root_url    = "http://203.0.113.7:3000" # as viewers see it (invite links)
    env_file    = "~/deploy/.env"          # holds the admin credentials
    admin_user_var     = "GRAFANA_ADMIN_USER"     # default user "admin"
    admin_password_var = "GRAFANA_ADMIN_PASSWORD"
    store       = "~/.local/share/boardkit"       # bundles land here

    [hosts.teamhost.datasources]     # logical name -> an existing uid (main org)
    main = "pg-main"

    [hosts.teamhost.datasource_defs.risk_client_a]   # created by boardkit, in any
    type     = "grafana-postgresql-datasource"        # org that needs it
    url      = "db.example:5432"
    database = "risk"
    user     = "risk_client_a_ro"
    password_var = "RISK_CLIENT_A_PG_PASSWORD"        # read from env_file on the host
    sslmode  = "require"

`env_file` may be a list of files (e.g. the deploy .env and a secrets file).

The admin credentials are read on the host, when it applies a bundle; they
never travel.
"""
from __future__ import annotations

import os
import shlex
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .grafana import Grafana


@dataclass
class Host:
    name: str
    serves: list[str]
    ssh: str
    grafana_url: str
    root_url: str
    env_file: str | list[str] | None = None
    admin_user_var: str = "GF_SECURITY_ADMIN_USER"
    admin_password_var: str = "GF_SECURITY_ADMIN_PASSWORD"
    store: str = "~/.local/share/boardkit"
    datasources: dict[str, str] = field(default_factory=dict)
    datasource_defs: dict[str, dict] = field(default_factory=dict)

    @property
    def store_path(self) -> Path:
        return Path(os.path.expanduser(self.store))

    def admin_env(self) -> dict[str, str]:
        env = dict(os.environ)
        files = [self.env_file] if isinstance(self.env_file, str) else (self.env_file or [])
        for f in files:
            env.update(read_env_file(Path(os.path.expanduser(f))))
        return env

    def grafana(self) -> Grafana:
        env = self.admin_env()
        pw = env.get(self.admin_password_var)
        if not pw:
            raise SystemExit(f"{self.admin_password_var} is not set on {self.name}")
        return Grafana(self.grafana_url, user=env.get(self.admin_user_var) or "admin",
                       password=pw)


@dataclass
class Inventory:
    self_name: str | None
    hosts: dict[str, Host]

    def for_audience(self, audience: str) -> Host:
        hits = [h for h in self.hosts.values() if audience in h.serves]
        if len(hits) != 1:
            raise SystemExit(f"{len(hits)} hosts serve audience {audience!r} in the "
                             "inventory; exactly one must")
        return hits[0]

    def is_self(self, host: Host) -> bool:
        return host.name == self.self_name


def read_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE lines as a shell would read them (quotes, comments)."""
    out = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.removeprefix("export ").strip()
        try:
            parts = shlex.split(_strip_comment(v))
        except ValueError:
            parts = [v]
        out[k] = parts[0] if parts else ""
    return out


def _strip_comment(v: str) -> str:
    """Bash starts a comment only at a word boundary, outside quotes:
    `a#b` is a value, `x # note` is x. (shlex's comments=True cuts both.)"""
    quote = None
    for i, ch in enumerate(v):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "#" and (i == 0 or v[i - 1].isspace()):
            return v[:i]
    return v


def load_inventory(path: str | Path | None = None) -> Inventory:
    path = Path(path or os.environ.get("BOARDKIT_HOSTS")
                or os.path.expanduser("~/.config/boardkit/hosts.toml"))
    if not path.is_file():
        raise SystemExit(f"no host inventory at {path} (see boardkit.hosts)")
    d = tomllib.loads(path.read_text())
    hosts = {}
    for name, h in d.get("hosts", {}).items():
        allowed = set(Host.__dataclass_fields__) - {"name"}
        extra = set(h) - allowed
        if extra:
            raise SystemExit(f"hosts.{name}: unknown keys {sorted(extra)}")
        hosts[name] = Host(name=name, **h)
    return Inventory(self_name=d.get("self"), hosts=hosts)
