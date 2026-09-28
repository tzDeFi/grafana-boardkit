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
    name = f"boardkit-e2e-{secrets.token_hex(4)}"
    subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", name, "-p", "127.0.0.1::3000",
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
        yield {"url": url, "password": pw, "name": name}
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


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
