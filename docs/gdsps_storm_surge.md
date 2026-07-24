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

- [ ] 1. Refined prompt doc + status tracker (this file)
- [ ] 2. `requirements.txt` pins + `gdsps_common.py` + tests
- [ ] 3. `gdsps_wms.py` + tests
- [ ] 4. `gdsps_wcs.py` + tests
- [ ] 5. `gdsps_datamart.py` + tests
- [ ] 6. `gdsps_thredds.py` (endpoint-gated) + tests
- [ ] 7. `gdsps_processing.py` + tests
- [ ] 8. `gdsps_export.py` + tests
- [ ] 9. `map_view.build_gdsps_overlay_layer` + tests
- [ ] 10. `app.py` sidebar + state + caching + `test_app_ui.py`
- [ ] 11. `README.md` + `CLAUDE.md` documentation
- [ ] 12. Full test run green + status finalized

## Limitations

- Exploratory visualization only — not a warning service. Official ECCC alerts
  and emergency guidance take precedence.
- SSH is total water level, not an engineering/chart datum; ETAS is storm-surge
  elevation. They are never interchanged.
- The live WMS overlay and real ECCC network paths are exercised by hand
  outside the automated suite; tests mock all HTTP.
