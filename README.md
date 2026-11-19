# boardkit

Deploy Grafana boards from one declaration per repo. **Who may open them is
Grafana's to decide, never boardkit's.**

```bash
pip install "boardkit @ git+https://github.com/tzDeFi/grafana-boardkit@v0.4.1"
boardkit plan   boards.toml      # validate; show what it publishes
boardkit deploy boards.toml      # ship to the audience's host and publish there
```

## Access is set in Grafana

Invite people, put them in teams, grant a folder or a board, choose a home
page: all of it in Grafana (Administration, Users and access, or a folder's
Permissions). boardkit never reads or changes any of it, so a deploy, a
re-apply or a new version cannot undo it.

This is enforced, not promised: `boardkit.grafana.Grafana` refuses any write to
users, org members, invites, teams, roles, permissions or home pages
(`AccessIsGrafanas`), and a declaration that carries `viewers`, `team` or `home`
is refused with a pointer here. (Before 0.4 boardkit converged access on every
deploy and on a host timer, and removed what an operator had added by hand.
Stop any such timer: `boardkit apply --all` and `--access-only` no longer exist,
so an old timer fails loudly instead of republishing boards.)

What boardkit still creates, once: a folder (Grafana's default permissions
apply to a new one: review them), an org, a datasource. Nothing else.

## The declaration (in the repo, next to its boards)

```toml
project     = "risk"
audience    = "internal"     # internal -> the team host; me -> the owner's own host
sensitivity = "sensitive"    # sensitive | open: a sensitive project refuses a host
                             # that lets anonymous visitors in

[datasources]                # reference used in the board JSON -> logical name
"${DS_MAIN}" = "main"

[[folders]]
uid   = "risk"
title = "Risk"
tabs  = true                 # boards with rows become real tabs

  [[folders.boards]]
  file = "grafana/book-a.json"
  preference_vars = ["loss"] # kept even under --reset-vars
```

### Orgs: the boundary for sensitive data

Grafana OSS lets any member of an org send hand-written SQL to any of that
org's datasources, so a board permission stops a person *opening* a board, not
*reading the data behind it*. Adding someone to an org adds them to its
datasources. When one audience must not read another's rows, give each its own
org and a database role that sees only its rows:

```toml
[[folders]]
uid = "risk"
org = "Risk"                          # the internal folder, in the project's own org
[folders.datasources]
"${DS_MAIN}" = "risk_all"             # created in that org from the host inventory

  [[folders.boards]]
  file = "grafana/book-a.json"
  org  = "Risk · client-a"            # isolated: also published into an org of its own
  [folders.boards.datasources]
  "${DS_MAIN}" = "risk_client_a"      # a role that sees only client A's rows
```

boardkit creates the orgs and their datasources. Who is a member of which org
is set in Grafana. `boardkit audit` lists every org's members next to its
datasources.

What a deploy does, on the host:

1. **Refuses** if the host does not fit: wrong audience, or a sensitive project
   on a host with anonymous access.
2. **Publishes** each board: the folder by uid, datasource references rewired to
   the host's own datasources (an unmapped one is an error), rows turned into
   real tabs if asked, the values viewers chose in dropdowns carried over, and
   the board **read back** and compared (panels, tabs, folder, and the time
   picker's range, timezone and quick ranges: a setting Grafana did not keep is
   a problem in the report).

A board's `timepicker.quick_ranges` (a list of `{display, from, to}`) is carried
into the real-tabs form. When a board sets it, Grafana uses that list in place
of its built-in presets, so declare every range the board should offer; an entry
with any other key is refused.

**A deploy overwrites the live board.** If people arrange a board by hand in
Grafana, keep its layout in the repo and sync it from the live board before
deploying.

## The host inventory (private, on each machine)

`~/.config/boardkit/hosts.toml` (or `$BOARDKIT_HOSTS`) names the hosts, which
audience each serves, how to reach it, where its admin credentials are and its
datasource uids. See `boardkit/hosts.py`. It is never committed anywhere public.

## Other commands

* `boardkit apply --project P` runs on the host itself and republishes one
  stored project.
* `boardkit audit` prints what the host's Grafana *enforces* (read-only):
  anonymous access, invite lifetime, accounts, teams, pending invites, every
  folder's grants and every org's members, datasources and boards.

Design, the tools compared before building this, and the Grafana behaviours it
relies on: [ARCHITECTURE.md](ARCHITECTURE.md).

## Tests

`pytest` runs the unit tests plus end-to-end journeys against a real Grafana
OSS container (docker): deploy; then act as the operator in Grafana's own admin
pages (users, a team, folder and board grants, a home page, an invite); deploy
and re-apply again and check not one of them changed; remove some and check they
stay removed.
