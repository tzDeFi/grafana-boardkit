"""Fast checks of each piece; test_e2e_grafana.py drives them together."""
from __future__ import annotations

import json

import pytest

from boardkit import decl as D
from boardkit.datasources import references, rewire
from boardkit.deploy import bundle, load_bundle, remote_apply, ship
from boardkit.hosts import Host, load_inventory, read_env_file
from boardkit.policy import datasource_warnings, host_errors, static_errors
from boardkit.publish import carry_over_variables

from .boards import plain, tabbed


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


def test_datasource_warning_counts_outsiders(tmp_path):
    d = D.from_dict(_decl(tmp_path), tmp_path)
    w = datasource_warnings(d, {"int@example.com", "m@example.com", "x@y.z"},
                            {"int@example.com", "m@example.com"}, {"main"})
    assert w and w[0].startswith("1 account(s)")
    assert datasource_warnings(d, {"a@b.c"}, {"a@b.c"}, {"main"}) == []


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
    assert calls[1][:3] == ["rsync", "-a", "--delete"]
    assert calls[1][-1] == "tm:.local/share/boardkit/tmp/risk/", "home-relative, no ~"
    assert "mv tmp/risk projects/risk" in calls[3][2]
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
