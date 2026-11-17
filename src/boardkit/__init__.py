"""boardkit: deploy Grafana boards. Who sees them is Grafana's to decide.

One declaration per repo (boardkit.decl) says what to deploy; the host
inventory (boardkit.hosts) says where each audience lives. boardkit never reads
or changes users, teams, org members or permissions (boardkit.grafana refuses
the writes). See README.md.
"""
__version__ = "0.4.0"
