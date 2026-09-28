# boardkit

Deploy Grafana boards and decide who sees them, from one declaration per repo.

```bash
pip install "boardkit @ git+https://github.com/tzDeFi/boardkit@v0.3.0"
boardkit plan   boards.toml      # validate; show the groups it will make
boardkit deploy boards.toml      # ship to the audience's host and apply there
```

## The declaration (in the repo, next to its boards)

```toml
project     = "risk"
audience    = "internal"     # internal -> the team host; me -> the owner's own host
sensitivity = "sensitive"    # sensitive | open

[datasources]                # reference used in the board JSON -> logical name
"${DS_MAIN}" = "main"

[[folders]]
uid     = "risk"
title   = "Risk"
viewers = ["ops@example.com"]     # the whole folder; "all" = every account (open only)
tabs    = true                    # boards with rows become real tabs

  [[folders.boards]]
  file    = "grafana/book-a.json"
  viewers = ["client-a@example.com"]   # this board only
  home    = true                       # it is their home page
  preference_vars = ["loss"]           # kept even under --reset-vars
```

### Orgs: the boundary for sensitive data

Grafana OSS lets any member of an org send hand-written SQL to any of that
org's datasources, so a board permission stops a viewer *opening* a board, not
*reading the data behind it*. When one audience must not read another's rows,
give each its own org and a database role that sees only its rows:

```toml
[[folders]]
uid = "risk"
org = "Risk"                          # the internal folder, in the project's own org
viewers = ["ops@example.com"]
[folders.datasources]
"${DS_MAIN}" = "risk_all"             # created in that org from the host inventory

  [[folders.boards]]
  file = "grafana/book-a.json"
  viewers = ["client-a@example.com"]
  org  = "Risk · client-a"            # isolated: also published into an org of its own
  home = true
  [folders.boards.datasources]
  "${DS_MAIN}" = "risk_client_a"      # a role that sees only client A's rows
```

The project owns the orgs it names (their members are exactly its viewers),
and someone who is only a viewer of isolated boards is taken out of the main
org. Every apply counts, per org, the accounts that could still query data
behind a board they cannot open.

What a deploy does, on the host:

1. **Refuses** if the declaration breaks its policy: a sensitive project on a
   host with anonymous access, or granting "all".
2. **Publishes** each board: the folder by uid, datasource references rewired to
   the host's own datasources (an unmapped one is an error), rows turned into
   real tabs if asked, the values viewers chose in dropdowns carried over, and
   the board **read back** and compared.
3. **Converges access**: one team per group, members exactly the declared
   emails, View on the folder or the board, the board as the team's home page;
   every other grant on the project's folders and boards removed (Admin-level
   grants stay). People are accounts **pinned by id**, never names: a declared address with
   no account gets one created at once (login = email = the address) and a
   one-time password in the report, for you to hand over (these hosts have no
   SMTP). An address already held by an account boardkit did not create is
   not seated until you vouch for it with `boardkit adopt EMAIL` on the host;
   a pinned account whose login or email has changed is unseated and
   reported. (Grafana lets any signed-in user rename their own login and
   email, so a name is never proof of who someone is.)

The host re-applies every stored project on a timer (`boardkit apply --all
--access-only`) so that people who register are seated without a redeploy.

## The host inventory (private, on each machine)

`~/.config/boardkit/hosts.toml` (or `$BOARDKIT_HOSTS`) names the hosts, which
audience each serves, how to reach it, where its admin credentials are and its
datasource uids. See `boardkit/hosts.py`. It is never committed anywhere public.

## Other commands

* `boardkit apply --project P | --all [--access-only]` runs on the host itself.
* `boardkit audit` prints what the host's Grafana *enforces*: anonymous access,
  invite lifetime, accounts, teams, pending invites and every folder's grants,
  with flags for what the policy forbids.

Design, the tools compared before building this, and the Grafana behaviours it
relies on: [ARCHITECTURE.md](ARCHITECTURE.md).

## Tests

`pytest` runs the unit tests plus an end-to-end journey against a real Grafana
OSS container (docker): deploy, register through the invite flow, seat, then log
in as each viewer and check what they can open.
