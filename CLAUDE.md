# CLAUDE.md — geo_stream

Streamlit app with an always-visible **CHS water-level** view and optional
**ECCC Coastal Flooding Risk Index** polygons. Drawing an ROI automatically
selects an operating CHS observation station inside the exact shape, or the
nearest station with an explicit outside-distance label. The same ROI can then
be used to fetch ECCC's rolling 30-day Datamart forecast archive, intersect its
daily snapshots as an inclusive range, intersect their polygons locally,
filter, inspect, animate, and download processed or raw data.

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
collection across the test modules. `tests/test_app_ui.py` also does
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
| `archive_range.py` | Inclusive max-30-day aggregation, per-date outcomes, global range caps, and strict raw range bundle. |
| `chs.py` | Hardened CHS/IWLS station catalogue and observation/prediction client, parsing, chart data, QC labels, and raw bundle. |
| `animation.py` | Pure Folium timeline preparation and rendering for one issuance's forecast validity times. |
| `geometry.py` | Shapely: ROI parsing/repair, exact station-point ranking, bbox extraction, per-feature clipping. All GEOS contact is here. |
| `properties.py` | Reading ECCC's dotted property paths, normalizing risk/contributors/datetimes, the DataFrame, the GeoJSON export. |
| `filtering.py` | Pure `FilterCriteria` matching + `summarize_features`. No I/O, no Streamlit. |
| `map_view.py` | Folium map, `Draw` toolbar, drawing rehydration, result layer, escaped popups/tooltips, legend, synthetic banner. |
| `state.py` | `reconcile_drawings` — validates the raw `all_drawings` payload from streamlit-folium into a `DrawingState`. |
| `synthetic.py` | Locally generated, loudly labelled fake features for UI work when an archive range is empty. |
| `gdsps_common.py` | GDSPS shared errors (`GDSPSError` under `ECCCError`), value dataclasses, the ETAS/SSH variable vocabulary, and bbox/time helpers. |
| `gdsps_wms.py` | Hardened GeoMet WMS `GetCapabilities` discovery of GDSPS layers + `time` dimensions, and validated overlay tile params. |
| `gdsps_wcs.py` | Hardened GeoMet WCS coverage discovery and ROI-subset `GetCoverage` NetCDF fetch; raises `GDSPSDataUnavailableError` to trigger the Datamart fallback. |
| `gdsps_datamart.py` | Hardened `model_gdsps/` directory crawl, NetCDF filename parsing, and byte download. Guaranteed numerical path. |
| `gdsps_thredds.py` | Optional OPeNDAP client, inert unless `GDSPS_THREDDS_CATALOG_URL` is set to a verified endpoint. |
| `gdsps_processing.py` | Pure Xarray/Shapely: open NetCDF bytes, bbox prefilter, exact ROI mask (`shapely.contains_xy`), time select, point series. No I/O. |
| `gdsps_export.py` | Model-neutral export ZIP (NetCDF, CSV, ROI GeoJSON, metadata JSON, README). No MIKE/Delft3D/SWAN/HEC-RAS. |
| `watch_and_run.py` | Dev supervisor. Not imported by the app. |

## Invariants — load-bearing, don't "simplify" them

- **`MAP_RETURNED_OBJECTS = ("all_drawings",)`.** Adding `bounds`, `zoom`, or
  `center` makes every pan trigger a Streamlit rerun, and the rerun recenters
  the map — a visible feedback loop. The viewport stays client-side.
- **`MAP_COMPONENT_KEY` is version-suffixed (`"coastal-flood-map-v5"`).** Bump
  the suffix whenever the map's structure changes; otherwise Streamlit reuses
  the stale mounted component and the change appears not to take.
- **CHS and ECCC state are independent.** CHS loads automatically and follows
  the drawing; ECCC remains button-triggered and continues to use
  `current_source_mode` for archive-vs-synthetic replacement. Never route CHS
  through `_store_dataset` or make a map rerun contact ECCC.
- **CHS station selection uses the exact repaired polygon.** An operating
  observation station covered by the ROI is preferred; otherwise the nearest
  point is selected and its distance outside the boundary is shown. Bedford
  Institute (00491) is only the no-drawing default.
- **CHS requests use 15-minute UTC buckets and cached outcomes.** A Streamlit
  drawing rerun must be a cache hit, including during a temporary API failure,
  so the app respects the published IWLS rate limits. Failed refreshes retain
  previously successful same-station data when possible. If a newly selected
  station has no usable bundle, the app may display the most recently
  successful other-station bundle, but must label both the failed station and
  fallback station explicitly and highlight the station supplying the chart.
- **GDSPS is discovery-based and opacity is download-free.** The WMS layer,
  WCS coverage, and Datamart filenames are discovered from live ECCC responses,
  never hardcoded; empty discovery is a plain "unavailable" message, not a
  fabricated overlay. The overlay reads WMS tiles client-side, so
  `gdsps_overlay_params` (including opacity) is rebuilt each rerun without any
  cached Python fetch — never route opacity through a `st.cache_data` fetch.
  Numerical retrieval is WCS-first, Datamart NetCDF fallback; ROI masking and
  time selection stay outside the byte caches. **ETAS (storm-surge elevation)
  and SSH (total water level) are never substituted, and SSH is never labelled
  an engineering/chart datum.**
- **Water-level semantics stay explicit.** `wlo` is an observation and `wlp`
  is a tide prediction. Never silently substitute one for the other, hide CHS
  QC/preliminary state, compare absolute heights from different station datums,
  or imply that a point gauge is an inundation map.
- **Drawings survive reruns via `_DrawingHydrator`, not via Folium.** Re-rendered
  polygons arrive in a throwaway `FeatureGroup`; the injected script moves them
  into Leaflet.Draw's `window.drawnItems` so they stay editable, guarded by a
  SHA-256 fingerprint of the serialized drawings so a rerun with unchanged
  drawings doesn't wipe an in-progress edit. Deleting either half loses the
  user's ROI on the next rerun. The `on_change` callback is primary, while the
  returned-payload fallback conditionally reruns once if delete-then-redraw
  exposed the preceding empty list; keep both paths.
- **Archive files do not accept a bbox; the exact ROI is applied locally.**
  `_cached_archive_fetch` downloads one selected date's static official files.
  The UI calls that day-level cache for every inclusive date, combines the
  successful snapshots, and only then lets `clip_feature_collection` honour the
  drawn polygon.
  `raw_feature_count` versus `clipped_feature_count` makes that visible.
- **`_cached_archive_fetch` takes `(archive_root, YYYYMMDD)`, never a client
  object, requests session, Shapely geometry, or ROI.** The exact ROI stays
  outside the cache and remains authoritative for clipping. Keep the cache
  day-level, including safe failure outcomes, so overlapping ranges and
  repeated temporary failures reuse the same bounded entries. A date-local 404
  may be retained while later dates continue; a systemic network, rate-limit,
  or service outcome must stop the remaining range requests.
- **Archive range outcomes distinguish not-loaded from empty.** A valid empty
  daily FeatureCollection counts as a successful date. Safe per-date failures
  may produce a partial range only when at least one date succeeds, are stored
  separately from geometry warnings, and appear in UI status and raw JSON.
  Zero successful dates keep the previous dataset. Repeated cross-issue
  features remain distinct; this is never described as an average. Enforce
  cumulative range-wide product and feature limits while adding daily outcomes,
  not only after every date has downloaded.
- **Partial exports remain visibly partial away from the app.** Raw range JSON
  carries the requested dates, per-date outcomes, and counts. A clipped GeoJSON
  filename includes an explicit partial marker and loaded-versus-requested
  count whenever any selected date was not loaded.
- **Animation never combines separate issuances into one frame.** A range can
  contain forecasts from many issue dates with the same validity time. Animate
  one issuance at a time; grouping only by validity would visually conflate
  forecasts that were produced at different times.
- **`archive.py`'s hardening is deliberate**: HTTPS-only root, no redirects,
  strict same-directory filename allowlist, highest-amendment selection,
  response media/type checks, file and feature ceilings, GET-only retries, and
  transactional failure. Do not loosen it. `api.py` remains similarly hardened
  for the current GeoMet view.
- **One malformed feature must never fail the whole response.**
  `clip_feature_collection` skips bad features individually, counts them, and
  returns warnings that the UI surfaces in an expander.
- **Every user-visible service error message comes from a safe
  `ECCCError`/`CHSError`/`GeometryError` subclass.** Unexpected exceptions get
  a generic message and a `LOGGER.exception`; raw exception text never reaches
  the UI. Failed fetches keep compatible previous results rather than blanking
  them.
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
  distinguishes an empty successfully loaded range, not-loaded dates,
  polygons that do not intersect the ROI, and features removed by filters.
- **Halifax 00490 currently has predictions but no IWLS `wlo` series.** Do not
  label its line observed. Bedford Institute 00491 is the default because it
  supplies same-station observations and predictions in Halifax Harbour.

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
