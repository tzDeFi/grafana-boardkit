# boardkit: architecture

## Decision 2026-11-19 (0.4.1): a dashboard link is carried, and read back

0.4.0 (and every version since the v2 converter) wrote `links: []` on every board it published
as v2, whatever the board said. A repo that generated a "Record AUM" link on each board shipped
boards without it, and the deploy read back "in sync": the check compared the panels, the tabs
and the time settings, not the links. The class is "a key of the classic board the converter
neither maps nor refuses, dropped without a word"; panels already refused that way, the
dashboard level did not.

So `links` now map key for key to v2 (Grafana's own conversion, pinned live, fills the keys a
link leaves out), a link the converter cannot map raises, and the read-back asks Grafana for
every declared link, in order, from the classic board (never from the converter's output), on
both publish paths. Still dropped at the dashboard level, unchanged: `refresh_intervals`,
`hidden`, and panel-level `links`.

## Decision 2026-11-17 (0.4.0): boardkit does not manage access

Until 0.3 boardkit also converged who may open each board (accounts, teams,
folder and board grants, home pages) on every deploy and on a host timer, and
took out whatever an operator had added by hand. The operator wants every
dashboard on the host controlled in Grafana's own admin pages and nowhere else:
Grafana already has users, teams and permissions, and a second system that
overwrites the first means an invite made in the UI is gone within minutes.

So access is out of boardkit entirely. It is made unreachable rather than
discouraged: the Grafana client refuses writes to the access endpoints
(`AccessIsGrafanas`), the declaration refuses `viewers`, `team` and `home`, and
there is no timer command left to point a cron at. An end-to-end test acts as the
operator in the UI, deploys and re-applies, and requires the access state to be
byte-identical, and what the operator removed to stay removed.

Kept on purpose: `org` (a boundary for datasources, which Grafana OSS draws per
org), the anonymous-host refusal for sensitive projects, and `audit` (read-only).
Dropped: the per-deploy "accounts that can query data behind boards they cannot
open" warning, which needed declared viewers; `boardkit audit` lists each org's
members next to its datasources instead.

Everything below this line that describes accounts, pinning, teams, grants or the
timer is the 0.3 design and is history.


## The problem it removes

Every repo that shipped a Grafana board carried its own publisher (five of them,
each with its own env var names for the datasource and folder), and access was
managed five different ways: a cron script for one project, a hand-run script
that put *every* Viewer into another project's team, invites made by hand in two
UIs, nginx password files, and sandbox Grafanas left anonymous. Two of those
scripts managed teams on the same host and contradicted each other.

boardkit is the one way: a repo declares **what** it deploys and **to whom**
(`boards.toml`), the host inventory says **where** each audience lives, and one
command converges the host on the declaration.

## Decisions

| Question | Decision | Why |
|---|---|---|
| Who says who sees a board | The repo that owns the board, in its declaration | The repo already knows its audience (a mandate's recipients, a partner's team); one file per repo, no central list to drift from it |
| Where a board lives | Its **audience**: `me` → the owner's own host, `internal` → the team host | Hosting follows who may look, not which repo built it |
| What sensitivity changes | `sensitive`: login-only host, explicit viewers, no builtin-role grants; `open`: may grant every account | The two classes the boards actually fall into |
| How it reaches a host | Publish **directly** to the host's Grafana; no cloud stack, no mirror sync | One user system per host instead of three; no sync layer to lag or lose tabs |
| Where credentials live | Only on the host: the bundle is shipped and applied there | Admin passwords never travel or sit in CI |
| Accounts for people without one | boardkit CREATES the account (login = email = the declared address), pins it by id and prints a one-time password to hand over; accounts it did not create are seated only after `boardkit adopt` | Grafana lets any signed-in user rename their own login and email, and an invitee picks their own username: resolving people by name let a viewer take an unregistered person's seat (review, 2026-09-28, reproduced). Invites leave the address free until the person registers; a created account never does |
| Dependencies | Python stdlib only | Runs on a host with nothing but `python3`; the deploy ships the library with the bundle, so host and repo never disagree on the version |
| Keeping one audience out of another's data | A Grafana org per audience, each with a database role restricted by row-level security; the project owns its orgs | Datasources are per org, and any org member can query them with hand-written SQL: no permission inside an org can stop that |
| Real tabs | Classic JSON is what repos generate and test; the v2 `TabsLayout` is produced at publish time and pinned to Grafana's own conversion by a live test | Tabs exist only in the v2 schema |

## Tools considered (compare-and-select, 2026-09-28)

| Candidate | Covers | Misses for this job | Verdict |
|---|---|---|---|
| **Terraform + grafana provider** | Teams, folder/dashboard permissions, dashboards, datasources, declaratively, with a plan | No invites (OSS users must be created with a password), a state file per host to keep safe, a Go toolchain plus a DSL for a one-operator setup, and nothing for dashboard v2 tabs or variable carry-over | Heavier than the problem; revisit if the number of hosts grows |
| **gcx / grafanactl** (Grafana Labs CLIs) | Push/pull of dashboards and folders through the app-platform (v2) APIs | No teams, members, permissions or invites; grafanactl is archived in favour of gcx (2026-06) | Covers half; would still need everything in `access.py` |
| **grafana-client** (Python) | Thin wrappers for most HTTP endpoints | A dependency on hosts that have no venv, and the endpoints are the easy part; the model (declaration, policy, convergence) is still ours | Not worth the dependency |
| **grafana-foundation-sdk / grafanalib** | Typed dashboard *generation* | Generation only: nothing deploys or manages access | Orthogonal: a repo may generate with it and deploy with boardkit |
| **An SSO proxy (Authelia / authentik) in front of every board** | One login for Grafana and web apps (dmonitor) | Team sync from IdP groups is Grafana Enterprise only, so per-board permissions still need the API; and a new always-on service | The right next step for *authentication* across apps; boardkit stays the place that decides *authorization* |

Pick: a small stdlib library, because the valuable part is the model
(audience → host, sensitivity → rules, declaration → exact grants) and its
tests against a real Grafana, not the HTTP calls.

## Grafana behaviours the tests pin (13.0.2)

* Any signed-in user can change their own login AND email (PUT /api/user) to
  any unused value, with no verification: a name is never an identity.
  Completing an invite with a different email is refused (400).

* A GF_* variable under the wrong section is accepted and ignored: read
  `/api/admin/settings`, never the compose file (`boardkit audit`).
* Any org member can POST `/api/ds/query` with hand-written SQL to any
  datasource in the org. Permissions stop a viewer *opening* a board, not
  querying its database. Sensitive projects get a warning with the head count;
  the fix is a Grafana org per audience or a row-restricted database role.
* A new grant reaches an account that is already active after up to ~60 s
  (Grafana caches resolved permissions); a removal is immediate.
* A classic board's `timepicker.quick_ranges` is v2 `timeSettings.quickRanges`
  (`{display, from, to}`, in order; an empty list is stored as none; any other
  key on an entry is dropped). The v2 PUT answers 200 whether or not the
  converter carried it, so a deploy compares the time range, timezone and quick
  ranges on read-back (`publish._time_problems`).
* Org role None cannot query datasources (every panel "Access denied"), so
  viewers are org Viewers.
