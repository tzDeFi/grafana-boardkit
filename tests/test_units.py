"""Fast checks of each piece; test_e2e_grafana.py drives them together."""
from __future__ import annotations

import copy
import json

import pytest

from boardkit import decl as D
from boardkit import publish
from boardkit.datasources import references, rewire
from boardkit.deploy import bundle, load_bundle, remote_apply, ship
from boardkit.hosts import Host, load_inventory, read_env_file
from boardkit.policy import host_errors, static_errors
from boardkit.publish import carry_over_variables, publish_board
from boardkit.v2 import to_v2

from .boards import QUICK_RANGES, plain, tabbed


def _decl(tmp_path, **over):
    (tmp_path / "a.json").write_text(json.dumps(tabbed("bk-a")))
    (tmp_path / "b.json").write_text(json.dumps(plain("bk-b")))
    d = {"project": "p", "audience": "internal", "sensitivity": "sensitive",
         "datasources": {"${DS_MAIN}": "main", "maindb": "main"},
         "folders": [{"uid": "f1", "title": "F", "viewers": ["Int@Example.com"],
                      "boards": [{"file": "a.json", "viewers": ["m@example.com"], "home": True},
                                 {"file": "b.json"}]}]}
    d.update(over)
    return d


# ── declaration ──────────────────────────────────────────────────────────────

def test_declaration_loads_and_names_its_teams(tmp_path):
    d = D.from_dict(_decl(tmp_path), tmp_path)
    f = d.folders[0]
    assert f.viewers == ["int@example.com"], "emails are normalised"
    assert d.folder_team(f) == "p · F"
    assert d.board_team(f.boards[0]) == "p · bk-a" and d.board_team(f.boards[1]) is None


@pytest.mark.parametrize("mutate, msg", [
    (lambda d: d.update(colour="red"), "unknown keys"),
    (lambda d: d["folders"][0].update(veiwers=[]), "unknown keys"),
    (lambda d: d["folders"][0]["boards"][0].update(viewers="all"), "list of emails"),
    (lambda d: d["folders"][0]["boards"][1].update(home=True), "home = true needs"),
    (lambda d: d.update(audience="everyone"), "audience"),
    (lambda d: d["folders"][0].update(uid="Has Spaces"), "slug"),
    (lambda d: d["folders"][0]["boards"][0].update(team="p · F"), "share a team name"),
    (lambda d: d["folders"][0]["boards"].append({"file": "a.json"}), "declared twice"),
    (lambda d: d["folders"][0]["boards"][0].update(file="nope.json"), "no board file"),
])
def test_declaration_rejects_what_would_silently_widen_access(tmp_path, mutate, msg):
    d = _decl(tmp_path)
    mutate(d)
    with pytest.raises(D.DeclarationError, match=msg):
        D.from_dict(d, tmp_path)


def test_sensitive_cannot_grant_all(tmp_path):
    d = _decl(tmp_path)
    d["folders"][0]["viewers"] = "all"
    assert static_errors(D.from_dict(d, tmp_path))
    d["sensitivity"] = "open"
    assert static_errors(D.from_dict(d, tmp_path)) == []


def test_toml_declaration(tmp_path):
    (tmp_path / "a.json").write_text(json.dumps(tabbed("bk-a")))
    (tmp_path / "boards.toml").write_text('''
project = "p"
audience = "me"
sensitivity = "open"
[datasources]
"${DS_MAIN}" = "main"
[[folders]]
uid = "f1"
viewers = "all"
  [[folders.boards]]
  file = "a.json"
''')
    d = D.load(tmp_path / "boards.toml")
    assert d.folders[0].viewers == "all" and d.folders[0].boards[0].uid == "bk-a"


# ── datasources ──────────────────────────────────────────────────────────────

def test_rewire_maps_every_reference_and_leaves_builtins():
    b = tabbed(ds_ref="${DS_MAIN}")
    b["__inputs"] = [{"name": "DS_MAIN"}]
    b["annotations"] = {"list": [{"datasource": {"type": "grafana", "uid": "-- Grafana --"}}]}
    b["templating"]["list"].append({"type": "datasource", "name": "ds", "query": "postgres"})
    assert references(b) == {"${DS_MAIN}"}
    out = rewire(b, {"${DS_MAIN}": {"type": "grafana-postgresql-datasource", "uid": "pg"}})
    assert "__inputs" not in out and references(out) == {"pg"}
    assert out["annotations"]["list"][0]["datasource"]["uid"] == "-- Grafana --"
    assert all(v["type"] != "datasource" for v in out["templating"]["list"])
    assert b["panels"][1]["datasource"]["uid"] == "${DS_MAIN}", "input untouched"


def test_rewire_refuses_an_unmapped_reference():
    with pytest.raises(ValueError, match=r"no datasource mapping for \['maindb'\]"):
        rewire(plain(ds_ref="maindb"), {"other": {"type": "x", "uid": "y"}})


# ── policy ───────────────────────────────────────────────────────────────────

HOST = Host(name="h", serves=["internal"], ssh="h", grafana_url="u", root_url="r")


def test_sensitive_boards_refuse_an_anonymous_host(tmp_path):
    d = D.from_dict(_decl(tmp_path), tmp_path)
    anon = {"auth.anonymous": {"enabled": "true"}}
    assert any("anonymous" in e for e in host_errors(d, HOST, anon))
    assert host_errors(d, HOST, {"auth.anonymous": {"enabled": "false"}}) == []
    d.audience = "me"
    assert any("not audience 'me'" in e for e in host_errors(d, HOST, {}))


def _iso(tmp_path, project="p"):
    d = _decl(tmp_path)
    d["project"] = project
    d["folders"][0]["org"] = "P"
    d["folders"][0]["boards"][0]["org"] = "P · a"
    d["folders"][0]["boards"][0]["datasources"] = {"${DS_MAIN}": "main_a"}
    return D.from_dict(d, tmp_path)


def test_placements_and_isolated_only(tmp_path):
    from boardkit.access import isolated_only, placement_viewers
    d = _iso(tmp_path)
    got = [(org, b.uid, iso, sorted(who)) for org, f, b, iso, who in placement_viewers(d)]
    assert got == [("P", "bk-a", False, ["int@example.com"]),
                   ("P · a", "bk-a", True, ["m@example.com"]),
                   ("P", "bk-b", False, ["int@example.com"])]
    assert d.datasource_map(d.folders[0], d.folders[0].boards[0], True)["${DS_MAIN}"] == "main_a"
    assert isolated_only([d]) == {"m@example.com"}
    # someone isolated here but a main-org viewer in another project stays in main
    (tmp_path / "o").mkdir()
    o = _decl(tmp_path / "o", project="q", sensitivity="open")
    o["folders"] = [{"uid": "q1", "viewers": ["m@example.com"], "boards": [{"file": "b.json"}]}]
    assert isolated_only([d, D.from_dict(o, tmp_path / "o")]) == set()


def test_isolation_rules(tmp_path):
    d = _decl(tmp_path)
    d["folders"][0]["boards"][1]["org"] = "X"
    with pytest.raises(D.DeclarationError, match="needs viewers"):
        D.from_dict(d, tmp_path)
    d = _decl(tmp_path)
    d["folders"][0]["org"] = "Main Org."
    with pytest.raises(D.DeclarationError, match="other than the main org"):
        D.from_dict(d, tmp_path)
    d = _decl(tmp_path)
    d["folders"][0]["boards"][0]["datasources"] = {"x": "y"}
    with pytest.raises(D.DeclarationError, match="only to an isolated board"):
        D.from_dict(d, tmp_path)


def test_exposure_counts_members_who_can_read_boards_they_cannot_open(tmp_path):
    from boardkit.deploy import _exposure
    shared = D.from_dict(_decl(tmp_path), tmp_path)      # both boards in the main org

    class G:
        """Org members by org id, from the X-Grafana-Org-Id-free admin API."""

        def __init__(self, by_org):
            self.by_org = by_org

        def __call__(self, path, method="GET", body=None, ok404=False):
            oid = int(path.split("/")[3])
            return [{"email": e, "role": "Viewer", "login": e, "userId": i}
                    for i, e in enumerate(self.by_org.get(oid, []))]

    w = _exposure(G({1: ["int@example.com", "m@example.com"]}), shared, {None: 1})
    assert len(w) == 1 and w[0].startswith("1 account(s) in org 'main'"), w  # m reads bk-b
    iso = _iso(tmp_path)
    ids = {"P": 2, "P · a": 3}
    assert _exposure(G({2: ["int@example.com"], 3: ["m@example.com"]}), iso, ids) == [], \
        "isolated: nobody can query rows behind a board they cannot open"
    leaked = _exposure(G({2: ["int@example.com", "m@example.com"], 3: ["m@example.com"]}),
                       iso, ids)
    assert len(leaked) == 1 and "org 'P'" in leaked[0], "m left in the folder's org is caught"


# ── hosts ────────────────────────────────────────────────────────────────────

def test_env_file_reads_like_a_shell(tmp_path):
    f = tmp_path / ".env"
    f.write_text('# c\nA=plain\nB="with space"\nexport C=\'q#uoted\'\nD=a#b\nE=x # note\n')
    assert read_env_file(f) == {"A": "plain", "B": "with space", "C": "q#uoted",
                                "D": "a#b", "E": "x"}


def test_inventory_rejects_unknown_keys_and_picks_one_host(tmp_path):
    p = tmp_path / "hosts.toml"
    p.write_text('self = "a"\n[hosts.a]\nserves=["me"]\nssh="a"\ngrafana_url="u"\n'
                 'root_url="r"\n[hosts.b]\nserves=["internal"]\nssh="b"\ngrafana_url="u"\n'
                 'root_url="r"\n')
    inv = load_inventory(p)
    assert inv.for_audience("internal").name == "b" and inv.is_self(inv.hosts["a"])
    p.write_text(p.read_text() + 'colour = "red"\n')
    with pytest.raises(SystemExit, match="unknown keys"):
        load_inventory(p)


# ── deploy transport ─────────────────────────────────────────────────────────

def test_bundle_round_trips(tmp_path):
    d = D.from_dict(_decl(tmp_path), tmp_path)
    out = bundle(d, tmp_path / "bundle")
    again = load_bundle(out)
    assert [b.uid for _, b in again.boards()] == ["bk-a", "bk-b"]
    assert again.folders[0].viewers == ["int@example.com"]


def test_ship_and_remote_apply_commands(tmp_path):
    calls = []

    class R:
        stdout = "[]"

    def run(cmd):
        calls.append(cmd)
        return R()

    h = Host(name="tm", serves=["internal"], ssh="tm", grafana_url="u", root_url="r")
    ship(h, tmp_path, "risk", run=run)
    assert "lib/boardkit/__init__.py" in calls[0][2], "reads the host's library version first"
    assert calls[2][:3] == ["rsync", "-a", "--delete"]
    assert calls[2][-1] == "tm:.local/share/boardkit/tmp/risk/", "home-relative, no ~"
    assert "mv tmp/risk projects/risk" in calls[4][2]
    remote_apply(h, "risk", access_only=True, reset_vars=False, run=run)
    assert calls[-1][:2] == ["ssh", "tm"]
    assert "PYTHONPATH=$HOME/.local/share/boardkit/lib python3 -m boardkit apply " \
           "--project risk --json --access-only" == calls[-1][2]


# ── publish: variable carry-over ─────────────────────────────────────────────

class _G:
    def __init__(self, live):
        self.live = live

    def __call__(self, path, method="GET", body=None, ok404=False):
        return {"dashboard": self.live} if self.live else None


def test_carry_over_keeps_a_viewers_choice_but_not_an_illegal_one():
    live = tabbed()
    live["templating"]["list"][0]["current"] = {"text": "1h", "value": "1h"}
    live["templating"]["list"][1]["current"] = {"text": "55", "value": "55"}
    new = tabbed()
    kept = carry_over_variables(_G(live), new)
    assert kept == ["freq=1h", "loss=55"]
    assert new["templating"]["list"][0]["options"][1]["selected"] is True
    new = tabbed()
    new["templating"]["list"][0]["options"] = [{"text": "1d", "value": "1d"}]
    assert carry_over_variables(_G(live), new, only={"freq"}) == []
    new = tabbed()
    assert carry_over_variables(_G(live), new, only={"loss"}) == ["loss=55"]
    assert carry_over_variables(_G(None), tabbed()) == []


def test_carry_over_multi_select_and_query_variables():
    """A multi-select holds a LIST (Grafana's "$__all" too), and a query variable
    declares no options (Grafana fills them at runtime). Neither may crash or be
    dropped: 2026-09-28, the EcoLiq boards' chain/venue pickers."""
    board = tabbed()
    board["templating"]["list"] = [
        {"type": "custom", "name": "venues", "multi": True, "query": "a,b,c",
         "current": {"text": ["a"], "value": ["a"]},
         "options": [{"text": t, "value": t, "selected": t == "a"} for t in "abc"]},
        {"type": "query", "name": "chain", "multi": True, "query": "select 1",
         "current": {"text": "All", "value": "$__all"}, "options": []}]
    live = tabbed()
    live["templating"]["list"] = [
        {"type": "custom", "name": "venues", "current": {"text": ["b", "c"], "value": ["b", "c"]}},
        {"type": "query", "name": "chain", "current": {"text": ["etherlink"],
                                                       "value": ["etherlink"]}}]
    kept = carry_over_variables(_G(live), board)
    assert kept == ["venues=['b', 'c']", "chain=['etherlink']"]
    v, c = board["templating"]["list"]
    assert v["current"] == {"text": ["b", "c"], "value": ["b", "c"]}
    assert [o["selected"] for o in v["options"]] == [False, True, True]
    assert c["current"]["value"] == ["etherlink"]
    # a pick that is no longer an option falls back to the generated default
    live["templating"]["list"][0]["current"] = {"text": ["z"], "value": ["z"]}
    fresh = tabbed()
    fresh["templating"]["list"] = [dict(board["templating"]["list"][0],
                                        current={"text": ["a"], "value": ["a"]})]
    assert carry_over_variables(_G(live), fresh) == []


def test_ship_refuses_to_downgrade_the_hosts_library(tmp_path, monkeypatch):
    """The host keeps ONE library for every stored project and its timer: a
    repo pinned to an older boardkit must not roll everyone back."""
    class R:
        stdout = '__version__ = "99.0.0"\n'

    h = Host(name="tm", serves=["internal"], ssh="tm", grafana_url="u", root_url="r")
    monkeypatch.delenv("BOARDKIT_ALLOW_DOWNGRADE", raising=False)
    with pytest.raises(SystemExit, match="newer than this"):
        ship(h, tmp_path, "risk", run=lambda cmd: R())


def test_text_panels_with_script_are_refused():
    from boardkit.policy import board_errors
    ok = plain()
    ok["panels"].append({"id": 9, "type": "text", "title": "embed", "options": {
        "mode": "html", "content": '<iframe src="http://h:8320/" style="border:0"></iframe>'}})
    assert board_errors(ok) == []
    for bad in ("<script>alert(1)</script>", '<img src=x onerror="steal()">',
                '<a href="javascript:steal()">x</a>'):
        b = plain()
        b["panels"].append({"id": 9, "type": "text", "title": "t",
                            "options": {"mode": "html", "content": bad}})
        assert board_errors(b), bad


def test_identities_pin_and_persist_privately(tmp_path):
    import os

    from boardkit.identities import Identities
    p = tmp_path / "identities.json"
    ids = Identities(p)
    ids.pin("a@example.com", 7)
    assert Identities(p).get("a@example.com") == 7
    assert oct(os.stat(p).st_mode & 0o777) == "0o600"
    assert ids.unpin("a@example.com") and Identities(p).get("a@example.com") is None


# ── v2: the time picker's quick ranges ───────────────────────────────────────

def _with_quick(board, quick=QUICK_RANGES):
    board["timepicker"] = {"quick_ranges": copy.deepcopy(quick)}
    return board


def test_quick_ranges_map_to_v2_in_the_declared_order():
    got = to_v2(_with_quick(tabbed()))["spec"]["timeSettings"]["quickRanges"]
    assert got == QUICK_RANGES
    assert [q["display"] for q in got] == ["Yesterday", "Last 2 hours", "Last 7 days"]
    b = _with_quick(tabbed())
    to_v2(b)["spec"]["timeSettings"]["quickRanges"][0]["display"] = "changed"
    assert b["timepicker"]["quick_ranges"] == QUICK_RANGES, "the output is a copy"


@pytest.mark.parametrize("picker", [None, "absent", {}, {"quick_ranges": []},
                                    {"quick_ranges": None}],
                         ids=["null", "absent", "empty", "empty-list", "none"])
def test_no_quick_ranges_leaves_the_output_as_it_was(picker):
    """A board that sets none keeps Grafana's built-in list: no quickRanges key,
    and the rest of timeSettings exactly what 0.3.1 sent."""
    b = tabbed()
    if picker != "absent":
        b["timepicker"] = picker
    assert to_v2(b) == to_v2(tabbed())
    assert to_v2(b)["spec"]["timeSettings"] == {
        "timezone": "utc", "from": "now-7d", "to": "now", "autoRefresh": "5m",
        "autoRefreshIntervals": ["5s", "10s", "30s", "1m", "5m", "15m", "30m", "1h", "2h", "1d"],
        "hideTimepicker": False, "fiscalYearStartMonth": 0}


def test_quick_ranges_change_nothing_else_in_the_resource():
    without, with_ = to_v2(tabbed(), "f1"), to_v2(_with_quick(tabbed()), "f1")
    assert with_["spec"]["timeSettings"].pop("quickRanges") == QUICK_RANGES
    assert with_ == without


@pytest.mark.parametrize("bad", [
    [{"display": "x", "from": "now-1h", "to": "now", "hidden": True}],
    [{"display": "x", "from": "now-1h"}],
    [QUICK_RANGES[0], {"display": "x", "from": "now-1h", "to": "now", "foo": 1}],
    {"display": "x", "from": "now-1h", "to": "now"},
    "now-1h",
    [["x", "now-1h", "now"]],
], ids=["unknown-key", "missing-key", "second-entry", "not-a-list", "string", "entry-not-a-dict"])
def test_a_quick_range_the_converter_cannot_map_raises(bad):
    """Grafana would drop what it cannot map without a word."""
    with pytest.raises(ValueError, match=r"timepicker\.quick_ranges"):
        to_v2(_with_quick(tabbed(), bad))


# ── publish: the time picker must read back as declared ──────────────────────

class _Grafana:
    """Just enough of Grafana, in memory: whatever is POSTed (classic) or POSTed
    and PUT (v2) is read back through `mangle`, as a Grafana that drops or
    changes a setting would. `mangle` gets the v2 `spec` or the classic board."""

    def __init__(self, mangle=lambda doc: None):
        self.v2 = self.classic = None
        self.mangle = mangle

    def namespace(self):
        return "default"

    def __call__(self, path, method="GET", body=None, ok404=False):
        if path.startswith("/apis/"):
            if method in ("POST", "PUT"):
                self.v2 = copy.deepcopy(body)
                return body
            if self.v2 is None:
                return None
            got = copy.deepcopy(self.v2)
            self.mangle(got["spec"])
            return got
        if method == "POST":
            self.classic = copy.deepcopy(body["dashboard"])
            return {}
        if self.classic is None:
            return None
        doc = copy.deepcopy(self.classic)
        self.mangle(doc)
        return {"meta": {"folderUid": "f1"}, "dashboard": doc}


def _problems(board, mangle=lambda doc: None, tabs=True):
    return publish_board(_Grafana(mangle), board, "f1", tabs=tabs)["problems"]


def _drop_quick(tabs):
    return ((lambda spec: spec["timeSettings"].pop("quickRanges")) if tabs
            else (lambda board: board.pop("timepicker")))


@pytest.mark.parametrize("tabs", [True, False], ids=["v2", "classic"])
def test_readback_passes_when_the_time_settings_come_back(tabs):
    make = tabbed if tabs else plain
    assert _problems(_with_quick(make()), tabs=tabs) == []
    assert _problems(make(), tabs=tabs) == [], "a board that declares none asks for none"


@pytest.mark.parametrize("tabs", [True, False], ids=["v2", "classic"])
def test_readback_fails_when_quick_ranges_are_missing(tabs):
    got = _problems(_with_quick(tabbed() if tabs else plain()), _drop_quick(tabs), tabs=tabs)
    assert len(got) == 1 and got[0].startswith("time setting quickRanges: read back None"), got


@pytest.mark.parametrize("tabs", [True, False], ids=["v2", "classic"])
def test_readback_fails_when_quick_ranges_come_back_reordered(tabs):
    reorder = ((lambda spec: spec["timeSettings"]["quickRanges"].reverse()) if tabs
               else (lambda board: board["timepicker"]["quick_ranges"].reverse()))
    [got] = _problems(_with_quick(tabbed() if tabs else plain()), reorder, tabs=tabs)
    assert got.startswith("time setting quickRanges: read back [")


def test_readback_fails_when_the_range_or_the_timezone_is_dropped_or_changed():
    def drop_timezone(spec):
        del spec["timeSettings"]["timezone"]

    def other_to(spec):
        spec["timeSettings"]["to"] = "now-1d"

    def other_from(spec):
        spec["timeSettings"]["from"] = "now-30d"

    for mangle, key in ((drop_timezone, "timezone"), (other_to, "to"), (other_from, "from")):
        [got] = _problems(tabbed(), mangle)
        assert got.startswith(f"time setting {key}: read back"), got


def test_a_converter_that_drops_quick_ranges_cannot_pass(monkeypatch):
    """The 0.3.1 incident: the converter loses the setting, Grafana stores what it
    was sent, and the read-back must still say so (the expectation comes from the
    declaration, not from what the converter produced)."""
    real = publish.to_v2

    def dropping(board, folder_uid=None):
        res = real(board, folder_uid)
        res["spec"]["timeSettings"].pop("quickRanges", None)
        return res

    monkeypatch.setattr(publish, "to_v2", dropping)
    [got] = _problems(_with_quick(tabbed()))
    assert got.startswith("time setting quickRanges: read back None"), got
