"""Who a declared email IS, pinned by Grafana user id.

A Grafana login and email are claims the user controls: anyone signed in can
rename both (PUT /api/user) to any unused value, and an invitee picks their
own username. Resolving a declared viewer by name at every apply would seat
whoever holds the name that day: a viewer who renames their login or email to
the address of someone declared but not yet registered takes that person's
seat (2026-09-28 review, reproduced on Grafana 13.0.2).

So boardkit never lets an address sit unclaimed and never re-resolves a seated
person by name:
  * a declared email with no account gets one CREATED by boardkit (login =
    email = the declared address), pinned here by id at once;
  * an address already held by an account boardkit did not create is not
    seated until the operator vouches for it (`boardkit adopt EMAIL`);
  * every later apply acts on the pinned id, and if that account's login or
    email no longer equals the declared address it is not seated (and is
    removed where the project owns membership) until someone looks.
"""
from __future__ import annotations

import json
import os
from pathlib import Path


class Identities:
    def __init__(self, path: Path | None):
        self.path = path
        self.data: dict[str, int] = {}
        if path is not None and path.is_file():
            self.data = {k: int(v) for k, v in json.loads(path.read_text()).items()}

    def get(self, email: str) -> int | None:
        return self.data.get(email)

    def pin(self, email: str, uid: int) -> None:
        self.data[email] = int(uid)
        self._save()

    def unpin(self, email: str) -> bool:
        if self.data.pop(email, None) is None:
            return False
        self._save()
        return True

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, sort_keys=True))
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)
