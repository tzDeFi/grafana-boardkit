from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

import pytest

GRAFANA_IMAGE = os.environ.get("BOARDKIT_E2E_IMAGE", "grafana/grafana-oss:13.0.2")


def _wait(url: str, timeout: float = 90) -> None:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(url + "/api/health", timeout=3) as r:
                if json.loads(r.read()).get("database") == "ok":
                    return
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError(f"Grafana at {url} did not come up")


@pytest.fixture(scope="session")
def grafana_container():
    """A throwaway Grafana OSS, login only, like a host serving sensitive boards.
    Skipped only where docker is absent; CI has docker, so it runs there."""
    if not shutil.which("docker"):
        pytest.skip("docker not available")
    pw = secrets.token_urlsafe(16)
    tag = secrets.token_hex(4)
    name, net, pg = f"boardkit-e2e-{tag}", f"boardkit-e2e-{tag}", f"boardkit-pg-{tag}"
    subprocess.run(["docker", "network", "create", net], check=True, capture_output=True)
    subprocess.run(["docker", "run", "-d", "--rm", "--name", pg, "--network", net,
                    "-e", "POSTGRES_PASSWORD=t", "-e", "POSTGRES_DB=book", "postgres:17"],
                   check=True, capture_output=True)
    subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", name, "--network", net,
         "-p", "127.0.0.1::3000",
         "-e", f"GF_SECURITY_ADMIN_PASSWORD={pw}",
         "-e", "GF_AUTH_ANONYMOUS_ENABLED=false",
         "-e", "GF_USERS_ALLOW_SIGN_UP=false",
         "-e", "GF_USERS_USER_INVITE_MAX_LIFETIME_DURATION=720h",
         "-e", "GF_ANALYTICS_REPORTING_ENABLED=false",
         "-e", "GF_PLUGINS_PREINSTALL_DISABLED=true",
         GRAFANA_IMAGE], check=True, capture_output=True)
    try:
        port = subprocess.run(["docker", "port", name, "3000/tcp"], check=True,
                              capture_output=True, text=True).stdout.split(":")[-1].strip()
        url = f"http://127.0.0.1:{port}"
        _wait(url)
        pg_pw = _seed_postgres(pg)
        yield {"url": url, "password": pw, "name": name, "pg_host": f"{pg}:5432",
               "pg_passwords": pg_pw}
    finally:
        subprocess.run(["docker", "rm", "-f", name, pg], capture_output=True, check=False)
        subprocess.run(["docker", "network", "rm", net], capture_output=True, check=False)


def _seed_postgres(container: str) -> dict[str, str]:
    """A book with two clients' rows and a role per audience: ro_all reads
    everything, ro_a only client a's rows (row-level security)."""
    pw = {"ro_all": secrets.token_urlsafe(10), "ro_a": secrets.token_urlsafe(10)}
    sql = f"""
CREATE TABLE book (account text, v int);
INSERT INTO book VALUES ('a', 1), ('a', 2), ('b', 3);
ALTER TABLE book ENABLE ROW LEVEL SECURITY;
CREATE ROLE ro_all LOGIN PASSWORD '{pw["ro_all"]}';
CREATE ROLE ro_a LOGIN PASSWORD '{pw["ro_a"]}';
GRANT SELECT ON book TO ro_all, ro_a;
CREATE POLICY all_rows ON book FOR SELECT TO ro_all USING (true);
CREATE POLICY own_rows ON book FOR SELECT TO ro_a USING (account = 'a');
"""
    for _ in range(60):
        r = subprocess.run(["docker", "exec", "-i", container, "psql", "-U", "postgres", "-d",
                            "book", "-v", "ON_ERROR_STOP=1"], input=sql, text=True,
                           capture_output=True, check=False)
        if r.returncode == 0:
            return pw
        time.sleep(1)
    raise RuntimeError(f"seeding postgres failed: {r.stderr[-500:]}")


@pytest.fixture()
def tmp_inventory(tmp_path, grafana_container, monkeypatch):
    """An inventory whose only host is the container, and is 'this machine'."""
    monkeypatch.setenv("E2E_ADMIN_PASSWORD", grafana_container["password"])
    inv = tmp_path / "hosts.toml"
    inv.write_text(f'''
self = "local"
[hosts.local]
serves = ["internal"]
ssh = "local"
grafana_url = "{grafana_container['url']}"
root_url = "{grafana_container['url']}"
admin_password_var = "E2E_ADMIN_PASSWORD"
store = "{tmp_path / 'store'}"
[hosts.local.datasources]
main = "pg-main"
''')
    return inv


ROOT = Path(__file__).resolve().parent
