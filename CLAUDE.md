# CLAUDE.md — geo_stream
New/changed Python lines must pass Ruff via the pre-commit hook; install it with `C:\Tools\.venv\Scripts\python.exe C:\Tools\tools_bootstrap\install_hooks.py`.

Streamlit app: an always-visible CHS water-level view for a drawn ROI, plus optional ECCC
Coastal Flooding Risk Index polygons from the rolling 30-day Datamart archive, and GDSPS/RESPS
storm-surge layers. Exploratory visualization only — **it is not a warning service**, and
every user-facing string (especially empty-result and synthetic-data messages) must keep that
unambiguous; don't soften them.

**This repo is public.** No secrets, tokens or internal links in commits;
`.streamlit/secrets.toml` is gitignored.

## Adding to this file

This file loads in full every session; length costs attention.

- If a rule here already covers the discovery, add nothing; sharpen the almost-right line
  instead of appending a section.
- Don't describe what the code already says (layout, field lists, call chains).
- A bug fix is not by itself a reason for a new instruction — prefer enforcing the lesson in
  code structure, validation or a test.
- Add only if all hold: durable and non-obvious; not already enforced or documented
  elsewhere; its absence would realistically cause a consequential mistake; expressible in
  one precise sentence; no existing line can be sharpened instead.
- This file is not a bug diary, a changelog, or a substitute for reading the code. Removing a
  line that no longer earns its place is as valuable as adding one.

## Running

Uses the shared `C:\Tools\.venv` (`setup_env.bat` refuses to create a second venv). From the
repo root:

```
C:\Tools\.venv\Scripts\python.exe -m pytest -q          # all tests are offline
C:\Tools\.venv\Scripts\python.exe -m streamlit run app.py
```

**Use `python -m pytest` from the repo root, never bare `pytest`** — there is no `conftest.py`
or `tests/__init__.py`, and the `AppTest` scripts `import app`, so imports resolve only via the
cwd. All `.bat` launchers bind **loopback only**; `GEO_STREAM_PORT` overrides 8501.

`app.py` holds orchestration and Streamlit widgets **only** — no geometry, HTTP or property
parsing; everything under `coastal_flood_explorer/` must stay importable without Streamlit.

## Invariants — load-bearing, don't "simplify" them

- **`MAP_RETURNED_OBJECTS = ("all_drawings",)`.** Adding `bounds`/`zoom`/`center` makes every
  pan rerun and recenter the map; the viewport stays client-side.
- **Bump the version suffix of `MAP_COMPONENT_KEY` whenever the map's structure changes**, or
  Streamlit reuses the stale mounted component.
- **CHS and ECCC state are independent.** CHS loads automatically and follows the drawing;
  ECCC stays button-triggered with `current_source_mode`. Never route CHS through
  `_store_dataset` or let a map rerun contact ECCC.
- **CHS station selection uses the exact repaired polygon**: an operating observation station
  inside the ROI wins, else the nearest, with its outside-distance shown; Bedford Institute
  (00491) is only the no-drawing default.
- **CHS requests use 15-minute UTC buckets with cached outcomes** (reruns hit the cache, even
  across a temporary API failure). A fallback to another station's last good bundle must
  **label both the failed and the fallback station** and mark which supplies the chart.
- **Water-level semantics stay explicit.** `wlo` is observation, `wlp` is tide prediction; never
  substitute one for the other, hide CHS QC/preliminary state, compare absolute heights across
  station datums, or imply a point gauge is an inundation map.
- **GDSPS is discovery-based.** WMS layers, WCS coverages and Datamart filenames come from live
  ECCC responses, never hardcoded; empty discovery is a plain "unavailable" message, never a
  fabricated overlay.
- **Never route overlay opacity through a `st.cache_data` fetch** — `gdsps_overlay_params` is
  rebuilt every rerun and tiles load client-side.
- **GDSPS numerics are Datamart-first** (the only real per-lead-time series; WCS has no time
  axis), falling back to the WCS latest slice. **RESPS uses WCS only and never falls back to
  the Datamart/GDSPS numbers.** ROI masking and time selection stay outside the byte caches.
- **GDSPS and RESPS are told apart by `classify_model`, never by the words "storm surge".**
  Discovery gates on `classify_model(...) is not None` plus, for WMS, a real `time` dimension;
  `is_gdsps_identifier` alone must not be a discovery gate. `find_coverage(model, variable,
  member)` keeps retrieval model- and member-exact.
- **ETAS (surge elevation) and SSH (total water level) are never substituted, and SSH is never
  labelled an engineering/chart datum.**
- **Drawings survive reruns via `_DrawingHydrator`**, which moves re-rendered polygons into
  Leaflet.Draw's `window.drawnItems`, guarded by a SHA-256 fingerprint so an unchanged rerun
  doesn't wipe an in-progress edit. Keep both the `on_change` path and the returned-payload
  fallback; removing either loses the ROI.
- **`_cached_archive_fetch` takes only `(archive_root, YYYYMMDD)`** — never a client, session,
  geometry or ROI. It is day-level and caches safe failure outcomes too. Archive files take no
  bbox: daily snapshots are combined first, then `clip_feature_collection` applies the exact ROI
  locally (`raw_feature_count` vs `clipped_feature_count`).
- **Range fetch outcomes:** a date-local 404 may be retained while later dates continue; a
  systemic network/rate-limit/service failure stops the remaining requests. Enforce cumulative
  product and feature limits **while** adding dates, not after.
- **Not-loaded is distinct from empty.** A valid empty FeatureCollection is a successful date
  and is not an all-clear; a partial range needs ≥1 successful date, keeps per-date failures
  separate from geometry warnings, and surfaces them in UI and raw JSON; zero successes keep the
  previous dataset. Cross-issue duplicates stay distinct and are never called an average.
- **Partial exports stay visibly partial**: raw range JSON carries requested dates, per-date
  outcomes and counts; a clipped GeoJSON filename carries a partial marker and loaded/requested
  count whenever any date failed.
- **Animation never combines separate issuances into one frame**, even at equal validity time.
- **`archive.py` and `api.py` hardening is deliberate** — HTTPS-only root, no redirects, strict
  same-directory filename allowlist, highest-amendment selection, media-type checks, file and
  feature ceilings, GET-only retries, transactional failure. Do not loosen it.
- **One malformed feature never fails the response**: `clip_feature_collection` skips, counts
  and warns per feature.
- **User-visible service errors come only from safe `ECCCError`/`CHSError`/`GeometryError`
  subclasses**; anything else gets a generic message plus `LOGGER.exception`, never raw
  exception text. Failed fetches keep compatible previous results.
- **Synthetic data replaces archive data, never mixes with it**, and stays labelled in
  `source_mode`, feature properties, layer name, banner, tooltip, popup, table `source` column
  and download filename — keep all of them.
- **`normalize_risk` is the single source of truth for risk**; unrecognized values become
  `"Unknown"`, a real member of `RISK_LEVELS` and `RISK_COLOURS`.
- **`get_property` is flattened-first at every level**; don't replace it with plain `dict`
  traversal.
- **All popup/tooltip content is HTML-escaped** (`html_value` / `_escaped`) — properties are
  third-party.
- **`_results_are_stale`** compares the current drawing to the fetched ROI with Shapely
  `.equals()` and disables download while they differ.

## Traps

- **`serialize_feature_collection` exists in both `geometry.py` (strict) and `properties.py`
  (lenient)**; `app.py` uses the `properties.py` one via `feature_collection_bytes`.
- **`synthetic.py` imports the private `geometry._polygonal_parts`**; renaming it breaks
  synthetic generation silently.
- Several modules end with backward-compatible aliases; use the primary name in new code.
- **Dependency pins are tight on purpose** (streamlit-folium 0.27.x, folium 0.20.x, Streamlit
  ≥1.60 — `st_folium`'s `feature_group_to_add`/`on_change`/`returned_objects` and
  `Draw(feature_group=…)` depend on them); bumping any needs the map exercised by hand.

## Conventions

Datetimes are aware UTC internally, rendered ISO-8601 with `Z`. `map_view.py` and
`properties.py` spell identifiers `colour` — match the file. Tests mirror modules one-to-one
and **mock all HTTP** (nothing may reach ECCC/CHS/GitHub); `generate_synthetic_data` takes an
injectable `clock`; UI tests use `streamlit.testing.v1.AppTest` with seeded `session_state`.
Commit subjects are short imperative sentences, no body; auto-commit completed work.

## Branching

All work happens on `main`. Do not create branches for agent work - commit and push straight to `main`. If an agent branch does turn up, fold it into `main`, prune it locally and on the remote, then push.
