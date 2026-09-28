"""A small Grafana HTTP client: token or basic auth, JSON in and out.

Stdlib only, so the same code runs in a repo's venv, in CI and on a host that
has nothing but python3. Errors carry the status code; callers that expect a
404 say so (`ok404=True`) instead of parsing messages.
"""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request


class GrafanaError(RuntimeError):
    def __init__(self, method: str, path: str, status: int, body: str):
        super().__init__(f"{method} {path} -> {status}: {body[:300]}")
        self.status = status
        self.body = body


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

    def user_by_email(self, email: str) -> dict | None:
        return self(f"/api/users/lookup?loginOrEmail={urllib.parse.quote(email)}", ok404=True)

    def team_by_name(self, name: str) -> dict | None:
        hits = self(f"/api/teams/search?perpage=1000&name={urllib.parse.quote(name)}")["teams"]
        return next((t for t in hits if t["name"] == name), None)


def q(s: str) -> str:
    return urllib.parse.quote(s, safe="")
