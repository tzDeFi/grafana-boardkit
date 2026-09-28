# boardkit — architecture

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
| Accounts for people without one | Viewer invite link returned by the deploy (no SMTP on the hosts); an access-only apply on a timer seats them once they register | Grafana OSS cannot attach a team to an invite |
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

* A GF_* variable under the wrong section is accepted and ignored: read
  `/api/admin/settings`, never the compose file (`boardkit audit`).
* Any org member can POST `/api/ds/query` with hand-written SQL to any
  datasource in the org. Permissions stop a viewer *opening* a board, not
  querying its database. Sensitive projects get a warning with the head count;
  the fix is a Grafana org per audience or a row-restricted database role.
* A new grant reaches an account that is already active after up to ~60 s
  (Grafana caches resolved permissions); a removal is immediate.
* Org role None cannot query datasources (every panel "Access denied"), so
  viewers are org Viewers.
