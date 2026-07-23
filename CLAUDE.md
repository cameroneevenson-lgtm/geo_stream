# CLAUDE.md — geo_stream

Streamlit app for exploring **ECCC Coastal Flooding Risk Index** polygons inside
a region the user draws on a map of Canada. Draw an ROI → choose an issue from
ECCC's rolling 30-day Datamart forecast archive → fetch its static GeoJSON files
→ intersect every returned polygon against the *exact* drawn shape locally →
filter, inspect, animate, and download clipped GeoJSON or the raw JSON bundle.

Exploratory visualization only. It is not a warning service, and every
user-facing string in the app is written to keep that unambiguous — especially
the empty-result and synthetic-data messages. Don't soften them.

**This repo is public** (`github.com/cameroneevenson-lgtm/geo_stream`). No
secrets, tokens, or internal links in commits. `.streamlit/secrets.toml` is
gitignored.

## Running

Uses the shared `C:\Tools\.venv`, not a project-local venv. From the repo root:

```
C:\Tools\.venv\Scripts\python.exe -m pytest -q          # all tests are offline
C:\Tools\.venv\Scripts\python.exe -m streamlit run app.py
```

**Use `python -m pytest`, not bare `pytest`.** There is no `conftest.py` and no
`tests/__init__.py`, so `import coastal_flood_explorer` only resolves because
`python -m` puts the cwd on `sys.path`. The bare `pytest` executable fails
collection on all 8 test modules. `tests/test_app_ui.py` also does
`AppTest.from_file("app.py")` with a relative path, so the cwd must be the repo
root either way.

`.bat` launchers wrap the same thing: `setup_env.bat` (install into the shared
venv, refuses to create a second one), `run_local.bat`, `run_local_watch.bat`
(supervisor in `watch_and_run.py` — full process restart on file change, more
reliable than Streamlit's own watcher for the Folium component),
`run_tunnel.bat` (health-check + confirm + TryCloudflare quick tunnel). All bind
**loopback only**; `GEO_STREAM_PORT` overrides 8501.

## Module roles

`app.py` is orchestration and Streamlit widgets **only** — no geometry, no HTTP,
no property parsing. Everything testable lives under `coastal_flood_explorer/`
and is importable without Streamlit.

| File | Role |
| --- | --- |
| `api.py` | Hardened client for the current GeoMet view; retained and tested although the main UI now uses the archive. |
| `archive.py` | Hardened Datamart directory/product client, amendment selection, merged collection, and raw bundle. |
| `archive_dates.py` | Pure UTC 30-day issue-date window helper. |
| `animation.py` | Pure Folium timeline preparation and rendering for fetched forecast validity times. |
| `geometry.py` | Shapely: ROI parsing/repair, bbox extraction, per-feature clipping. All GEOS contact is here. |
| `properties.py` | Reading ECCC's dotted property paths, normalizing risk/contributors/datetimes, the DataFrame, the GeoJSON export. |
| `filtering.py` | Pure `FilterCriteria` matching + `summarize_features`. No I/O, no Streamlit. |
| `map_view.py` | Folium map, `Draw` toolbar, drawing rehydration, result layer, escaped popups/tooltips, legend, synthetic banner. |
| `state.py` | `reconcile_drawings` — validates the raw `all_drawings` payload from streamlit-folium into a `DrawingState`. |
| `synthetic.py` | Locally generated, loudly labelled fake features for UI work when an archive issue is empty. |
| `watch_and_run.py` | Dev supervisor. Not imported by the app. |

## Invariants — load-bearing, don't "simplify" them

- **`MAP_RETURNED_OBJECTS = ("all_drawings",)`.** Adding `bounds`, `zoom`, or
  `center` makes every pan trigger a Streamlit rerun, and the rerun recenters
  the map — a visible feedback loop. The viewport stays client-side.
- **`MAP_COMPONENT_KEY` is version-suffixed (`"coastal-flood-map-v4"`).** Bump
  the suffix whenever the map's structure changes; otherwise Streamlit reuses
  the stale mounted component and the change appears not to take.
- **Drawings survive reruns via `_DrawingHydrator`, not via Folium.** Re-rendered
  polygons arrive in a throwaway `FeatureGroup`; the injected script moves them
  into Leaflet.Draw's `window.drawnItems` so they stay editable, guarded by a
  SHA-256 fingerprint of the serialized drawings so a rerun with unchanged
  drawings doesn't wipe an in-progress edit. Deleting either half loses the
  user's ROI on the next rerun.
- **Archive files do not accept a bbox; the exact ROI is applied locally.**
  `_cached_archive_fetch` downloads the selected date's static official files,
  then `clip_feature_collection` honours the drawn polygon.
  `raw_feature_count` versus `clipped_feature_count` makes that visible.
- **`_cached_archive_fetch` takes `(archive_root, YYYYMMDD)`, never a client
  object, requests session, Shapely geometry, or ROI.** The exact ROI stays
  outside the cache and remains authoritative for clipping.
- **`archive.py`'s hardening is deliberate**: HTTPS-only root, no redirects,
  strict same-directory filename allowlist, highest-amendment selection,
  response media/type checks, file and feature ceilings, GET-only retries, and
  transactional failure. Do not loosen it. `api.py` remains similarly hardened
  for the current GeoMet view.
- **One malformed feature must never fail the whole response.**
  `clip_feature_collection` skips bad features individually, counts them, and
  returns warnings that the UI surfaces in an expander.
- **Every user-visible error message comes from an `ECCCError`/`GeometryError`
  subclass and is written to be shown verbatim.** Unexpected exceptions get a
  generic message and a `LOGGER.exception`; raw exception text never reaches
  the UI. Failed fetches keep the previous results rather than blanking them.
- **Synthetic data replaces archive data, never mixes with it.** It is labelled in
  `source_mode`, the feature properties, the map layer name, a fixed banner, the
  tooltip, the popup, the table's `source` column, and the download filename.
  Keep all of them.
- **`normalize_risk` is the single source of truth for risk.** It accepts the
  numeric 1–4 ECCC uses *and* string labels, and everything unrecognized becomes
  `"Unknown"` — which is a real member of `RISK_LEVELS` and `RISK_COLOURS`, so
  colour lookup and sort order can't `KeyError`.
- **`get_property` is flattened-first at every level.** ECCC returns
  `metobject.risk.value` flattened in practice, but the same function handles
  nested and mixed (`{"metobject": {"risk.value": 3}}`) shapes. Don't replace it
  with a plain `dict` traversal.
- **All popup/tooltip content is HTML-escaped** (`html_value` / `_escaped`).
  Feature properties come from a third party and land in raw HTML.
- **Results go stale, not wrong.** `_results_are_stale` compares the current
  drawing to the ROI the data was fetched for using Shapely `.equals()`, and
  disables the download while they differ.

## Traps

- **`serialize_feature_collection` exists twice** — in `geometry.py` (indented,
  `sanitize_for_json`, raises on a non-FeatureCollection) and in
  `properties.py` (compact, lenient, `json_safe`). `app.py` uses the
  `properties.py` one via `feature_collection_bytes`. Check which you're
  importing.
- **`synthetic.py` imports the private `geometry._polygonal_parts`.** Renaming it
  breaks synthetic generation, which the type checker won't flag.
- **Several modules end with backward-compatible aliases** (`extract_bbox`,
  `apply_filters`, `property_value`, `geojson_bytes`, …). They're thin
  re-exports; prefer the primary name in new code.
- **The dependency pins are tight for a reason.** `st_folium(...)`'s
  `feature_group_to_add`, `on_change`, and `returned_objects` need
  streamlit-folium 0.27.x; `folium.template.Template` and the `Draw(feature_group=…)`
  argument need folium 0.20.x; `width="stretch"` on buttons and dataframes needs
  Streamlit ≥1.60. Bumping any of the three needs the map exercised by hand.
- **An empty result is normal.** Official archive files may contain zero
  features. That is not a bug and not an all-clear — `_render_results`
  distinguishes an empty issue, polygons that do not intersect the ROI, and
  features removed by filters.

## Conventions

`from __future__ import annotations` everywhere, PEP 604 unions, full docstrings,
frozen slotted dataclasses for value types, defensive `isinstance` guards on
anything crossing a boundary (API response, session state, component payload),
~85-column lines. Datetimes are aware UTC internally and rendered ISO-8601 with
a `Z` suffix. Note the spelling split: identifiers use `colour` in `map_view.py`
and `properties.py` (`RISK_COLOURS`, `risk_colour`) — match the file you're in.

Tests mirror the modules one-to-one and **mock all HTTP** — nothing may reach the
ECCC service. `synthetic.generate_synthetic_data` takes an injectable
`clock` so generated timestamps are deterministic. UI behaviour is tested through
`streamlit.testing.v1.AppTest` by seeding `session_state` directly.

Commit subjects are short imperative sentences ("Constrain map navigation to
Canada"), no body. Auto-commit completed work without being asked.
