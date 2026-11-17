"""A small Grafana HTTP client: token or basic auth, JSON in and out.

Stdlib only, so the same code runs in a repo's venv, in CI and on a host that
has nothing but python3. Errors carry the status code; callers that expect a
404 say so (`ok404=True`) instead of parsing messages.
"""
from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.parse
import urllib.request


class GrafanaError(RuntimeError):
    def __init__(self, method: str, path: str, status: int, body: str):
        super().__init__(f"{method} {path} -> {status}: {body[:300]}")
        self.status = status
        self.body = body


class AccessIsGrafanas(RuntimeError):
    """boardkit tried to change who can see something. It never does."""


#: Everything that decides who may open what: users, org members, invites, teams,
#: roles and permissions, folder and board permissions, home pages. Read-only for
#: boardkit (the audit reads them); a write is refused before it leaves the process.
_ACCESS_PATHS = re.compile(
    r"^/api/(users|admin/users|org/users|orgs/[^/?]+/users|org/invites|invites|teams|"
    r"access-control|serviceaccounts|org/preferences|user/preferences|preferences)(/|\?|$)"
    r"|/permissions(/|\?|$)|/preferences(/|\?|$)")


def _refuse_access_write(method: str, path: str) -> None:
    if method.upper() not in ("GET", "HEAD") and _ACCESS_PATHS.search(path):
        raise AccessIsGrafanas(
            f"{method} {path}: boardkit never changes who can see a board. Do it in "
            "Grafana (Administration, Users and access).")


class Grafana:
    """`Grafana(url, token=...)` or `Grafana(url, user=..., password=...)`."""

    def __init__(self, url: str, *, token: str | None = None, user: str | None = None,
                 password: str | None = None, org_id: int | None = None,
                 timeout: float = 30):
        if not token and not (user and password):
            raise ValueError("Grafana needs a token, or a user and a password")
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if token:
            self.headers["Authorization"] = f"Bearer {token}"
        else:
            raw = f"{user}:{password}".encode()
            self.headers["Authorization"] = "Basic " + base64.b64encode(raw).decode()
        if org_id is not None:
            self.headers["X-Grafana-Org-Id"] = str(org_id)

    def __call__(self, path: str, method: str = "GET", body=None, *, ok404: bool = False):
        _refuse_access_write(method, path)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers=self.headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            if ok404 and e.code == 404:
                return None
            raise GrafanaError(method, path, e.code, e.read().decode(errors="replace")) from None
        except urllib.error.URLError as e:
            raise GrafanaError(method, path, 0, f"cannot reach {self.url}: {e.reason}") from None

    def in_org(self, org_id: int | None) -> Grafana:
        """The same client, acting in another org (X-Grafana-Org-Id). The
        account must be a member of it; boardkit's admin is, as its creator."""
        g = object.__new__(Grafana)
        g.url, g.timeout, g.headers = self.url, self.timeout, dict(self.headers)
        g.headers.pop("X-Grafana-Org-Id", None)
        if org_id is not None:
            g.headers["X-Grafana-Org-Id"] = str(org_id)
        return g

    # ── small helpers everything uses ──────────────────────────────────────

    def settings(self) -> dict:
        """Effective server settings (server admin only). The truth about what
        Grafana enforces: a GF_* variable under the wrong section is silently
        filed there and ignored."""
        return self("/api/admin/settings")

    def namespace(self) -> str:
        """k8s namespace of the app-platform APIs: `default` on OSS org 1."""
        return self("/api/frontend/settings").get("namespace") or "default"


def q(s: str) -> str:
    return urllib.parse.quote(s, safe="")
