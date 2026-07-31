"""geo_stream - coastal flood explorer.

The package marker exists so `coastal_flood_explorer` is reachable as
`geo_stream.coastal_flood_explorer` from anywhere on the shared venv, rather
than only when this repo's own directory happens to be on sys.path. Intra-repo
imports stay flat (`from coastal_flood_explorer import ...`), which keeps app.py
runnable as the script the .bat launchers invoke.
"""
