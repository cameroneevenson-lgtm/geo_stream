# GDSPS Storm Surge Support — Refined Task & Status

This document is the working specification and running status tracker for
adding Environment and Climate Change Canada's **Global Deterministic Storm
Surge Prediction System (GDSPS)** to `geo_stream` as a first-class data source
alongside CHS water levels and the Coastal Flooding Risk Index.

It is committed to the repository and updated as each piece of work lands.

## Goal

Let a user draw an ROI, enable a **GDSPS Storm Surge** WMS overlay on the
existing interactive map, pick a model run and forecast-valid time, fetch the
numerical data for the ROI, and download a model-neutral export package. The
map is the primary visualization — there is **no** separate Matplotlib/Plotly
graph.

## Variables (never conflated)

| Code | Meaning | Notes |
| --- | --- | --- |
| **ETAS** | Storm-surge elevation | Derived from SSH by harmonic analysis. |
| **SSH** | Total water level (sea-surface height) | **Not** an engineering datum. |

The two are treated as distinct throughout: separate selection, separate
retrieval, separate labelling in the export. One is never substituted for the
other, and SSH is never presented as an engineering/chart datum.

## Official data sources

Only documented ECCC/MSC services are used.

- **GeoMet WMS** — `https://geo.weather.gc.ca/geomet` (`SERVICE=WMS`,
  `VERSION=1.3.0`). Map overlay + layer/time discovery via `GetCapabilities`.
- **GeoMet WCS** — same host, `SERVICE=WCS` (`VERSION=2.0.1`). Preferred
  numerical retrieval when a matching coverage exists.
- **MSC Datamart NetCDF** — `https://dd.weather.gc.ca/model_gdsps/`. Numerical
  fallback when WCS cannot provide the requested variable, and the guaranteed
  numeric path.
- **THREDDS / OPeNDAP** — **not assumed to exist.** A client module
  (`gdsps_thredds.py`) is present but **inert by default**: it activates only
  when `GDSPS_THREDDS_CATALOG_URL` is set to a confirmed official HTTPS
  catalog. No unconfirmed THREDDS host is hardcoded. See *Design notes*.

## Numerical retrieval

- WCS `GetCoverage` whenever practical (`FORMAT=NetCDF`, `SUBSET` on
  long/lat/time) — retrieves only the requested region and forecast period.
- Datamart NetCDF download when WCS has no matching coverage.
- Xarray is used to open NetCDF, subset, mask to the exact ROI polygon,
  preserve metadata, and export. The full global forecast grid is never
  loaded — bbox prefiltering happens before any `.load()`.

## Streamlit integration

New sidebar section **GDSPS Storm Surge** with: enable overlay, refresh
available runs, model run, forecast-valid time, variable (ETAS/SSH where
available), overlay opacity, fetch numerical subset, download export. The WMS
overlay appears alongside existing map layers. Changing opacity never triggers
another download (WMS tiles are fetched client-side by the browser; opacity is
session-state only and is not part of any cache key).

## Export package (model-neutral ZIP)

- NetCDF subset
- CSV point time series
- ROI GeoJSON
- metadata JSON (run, variable + ETAS-vs-SSH definition, source service, bbox,
  generation time)
- README

No native MIKE, Delft3D, SWAN, or HEC-RAS files are generated.

## Code organization

Networking, parsing, Xarray processing, export, and UI stay separate. Nothing
GDSPS lives in `app.py` beyond orchestration/widgets, matching the existing
module split. New modules under `coastal_flood_explorer/`:

- `gdsps_common.py` — errors, dataclasses, variable definitions.
- `gdsps_wms.py` — WMS capabilities discovery + tile params.
- `gdsps_wcs.py` — WCS coverage discovery + `GetCoverage`.
- `gdsps_datamart.py` — Datamart directory crawl + NetCDF download.
- `gdsps_thredds.py` — endpoint-gated OPeNDAP client (inert by default).
- `gdsps_processing.py` — pure Xarray/Shapely subset + mask + point series.
- `gdsps_export.py` — model-neutral ZIP builder.
- `map_view.build_gdsps_overlay_layer` — WMS overlay Folium layer.

## Testing

Offline tests mirror the modules one-to-one and mock all HTTP; no live network
or OPeNDAP call runs in the suite. Coverage: WMS capabilities parsing, WCS
requests, Datamart downloads, THREDDS catalog parsing (mocked), Xarray
processing, ROI masking, export generation, map overlay, and UI states via
`AppTest`.

## Design notes

- **Discovery-based clients.** The exact GDSPS WMS layer name / WCS coverage
  ID could not be independently confirmed from public docs (ECCC GDSPS readme
  pages were not retrievable through the build proxy, and a code search of
  `ECCC-MSC/geomet-data-registry` / `geomet-mapfile` for "gdsps" returned no
  matches). Rather than hardcode a possibly-wrong layer name, the WMS/WCS/
  Datamart clients **discover** GDSPS content from the live
  GetCapabilities/directory response and pattern-match on
  GDSPS/StormSurge/ETAS/SSH — the same philosophy `archive.py` already uses to
  discover Coastal Flooding products from a Datamart directory listing. If
  nothing matches, the UI says so plainly and never fabricates an overlay.
- **THREDDS is opt-in only.** No official GDSPS OPeNDAP endpoint was
  confirmed. The client stays disabled until an operator sets a verified
  catalog URL, satisfying the "document before using" requirement.
- **Confirmed hosts.** `dd.weather.gc.ca` (already the Datamart host in
  `archive.py`) serves `model_gdsps/`; `geo.weather.gc.ca/geomet` is the live
  GeoMet OGC endpoint.

## Status

- [x] 1. Refined prompt doc + status tracker (this file)
- [x] 2. `requirements.txt` pins + `gdsps_common.py` + tests
- [x] 3. `gdsps_wms.py` + tests
- [x] 4. `gdsps_wcs.py` + tests
- [x] 5. `gdsps_datamart.py` + tests
- [x] 6. `gdsps_thredds.py` (endpoint-gated) + tests
- [x] 7. `gdsps_processing.py` + tests
- [x] 8. `gdsps_export.py` + tests
- [x] 9. `map_view.build_gdsps_overlay_layer` + tests
- [x] 10. `app.py` sidebar + state + caching + `test_app_ui.py`
- [x] 11. `README.md` + `CLAUDE.md` documentation
- [x] 12. Full test run green + status finalized

All GDSPS work is complete. The full offline suite passes (430 passed) apart
from one pre-existing, unrelated `test_chs.py` assertion that pins
`datetime64[ns, UTC]` while this environment's newer pandas produces `us`
resolution; that failure predates this feature and is not touched here. The
GDSPS modules add 75 dedicated offline tests, all mocking HTTP with no live
network or OPeNDAP calls.

## Live discovery verification (2026-07-24)

The discovery clients were run against the **live** GeoMet WMS/WCS service for
the first time and the layer/coverage identifiers are now confirmed, resolving
the earlier "layer name could not be confirmed" caveat:

- **GDSPS (deterministic):** `GDSPS_15km_StormSurge` = *GDSPS.ETAS – Storm surge
  [m]*; `GDSPS_15km_SeaSfcHeight` = *GDSPS.SSH – Sea surface height above Mean
  Water Level [m]*. Same IDs serve as WCS coverage IDs. Datamart: `model_gdsps/`.
- **RESPS (Regional Ensemble, Atlantic North-West):**
  `RESPS-Atlantic-North-West_9km_{StormSurge,SeaSfcHeight}_NN` for members
  `01`–`21` (member 01 = control). A **different model** from GDSPS.

**Bug found and fixed.** `is_gdsps_identifier` (token
`storm[\s_-]*surge|gdsps|ETAS|SSH`) was used as the discovery gate and, live,
returned **44 WMS layers where only the 2 GDSPS layers were intended** — it
swept in all 42 RESPS ensemble layers, the GDSPS/RESPS group containers, the
footprint outline, and bare `Storm_Surge-Dis` / `StormSurge_-3-3` legend
styles. Discovery now gates on `classify_model()` (anchored to the model
acronym, so GDSPS and RESPS are never conflated) plus, for WMS, a real `time`
dimension (drops groups/footprint/styles). `GDSPSLayerInfo` /
`GDSPSCoverageInfo` gained `model` + `member`; the sidebar picks
model → variable → (RESPS) member and the overlay is labelled per model.

Per the "keep both, clearly separated" decision, **RESPS is supported as a
distinct labelled model** (overlay + WCS coverage). RESPS **numerical subset /
export is not yet wired** — it needs its own Datamart tree and per-member
ensemble handling — so the fetch button is offered for GDSPS only and never
fetches GDSPS numbers under a RESPS selection. Regression tests using the real
capabilities shape assert exactly the intended data layers survive with correct
model/member tags (`tests/test_gdsps_wms.py`, `tests/test_gdsps_common.py`,
`tests/test_gdsps_service.py`).

## Limitations

- Exploratory visualization only — not a warning service. Official ECCC alerts
  and emergency guidance take precedence.
- SSH is total water level, not an engineering/chart datum; ETAS is storm-surge
  elevation. They are never interchanged. GDSPS (deterministic) and RESPS
  (ensemble) are separate models and are never mixed or averaged.
- RESPS numerical retrieval/export is not implemented; RESPS is overlay-only.
- The live WMS overlay and real ECCC network paths are exercised by hand
  outside the automated suite; tests mock all HTTP.
