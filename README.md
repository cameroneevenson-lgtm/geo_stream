# Geo Stream Coastal Flood Explorer

Geo Stream is an exploratory Streamlit application for viewing recent Canadian
Hydrographic Service (CHS) water levels and optional Environment and Climate
Change Canada (ECCC) Coastal Flooding Risk Index polygons. A user-drawn region
automatically selects an official water-level station and also defines the
exact area used for ECCC polygon clipping.

> **Screenshot placeholder:** add an application screenshot here after the
> first visual release.

The application is not an official warning service. Official ECCC weather
alerts and emergency guidance always take precedence.

## Data sources

### CHS water levels

The always-visible water-level panel uses the official [Integrated Water Level
System (IWLS) API](https://api-sine.dfo-mpo.gc.ca/swagger-ui/index.html). Before
a region is drawn, it defaults to Bedford Institute (00491) in Halifax Harbour.
After a polygon or rectangle is drawn, Geo Stream automatically selects an
operating CHS observation station inside the exact shape. If none is inside, it
uses the nearest station and reports how far that point lies outside the ROI.

The chart requests the station's recent official observations and, when that
station publishes them, its tide predictions. Heights are in metres relative
to the station's local datum; values from different stations must not be
treated as directly interchangeable. CHS quality-control flags and preliminary
status remain visible. Station measurements are point samples, not flood-depth
or street-level inundation maps. If a newly selected station has no usable
recent series or its request fails, the app keeps the most recently successful
station bundle visible and labels it as fallback data rather than silently
presenting it as data for the drawn region.

CHS documents the service, licence, quality flags, and request limits on its
[web-services page](https://www.tides.gc.ca/en/web-services-offered-canadian-hydrographic-service).
The application caches the station catalogue and 15-minute time series so map
reruns stay within the published limit of 3 requests per second and 30 per
minute.

### ECCC coastal-flood forecasts

The application uses ECCC's official rolling MSC Datamart forecast archive:

<https://dd.weather.gc.ca/YYYYMMDD/WXO-DD/coastal-flooding/risk-index/>

ECCC currently retains about 30 days of Datamart files. These are archived
forecasts, not observed flood events, a 30-day average, inundation maps, or a
permanent hazard layer. The current GeoMet collection still exists, but its
current-publication view can be globally empty; Geo Stream therefore asks for
an explicit recent archive issue-date range instead. The default range covers
all 30 retained daily partitions and preserves each forecast snapshot
independently rather than averaging them.

The [Coastal Flooding Risk Index Datamart
documentation](https://eccc-msc.github.io/open-data/msc-data/coastal-flooding/readme_coastal-flooding-risk-index-datamart_en/)
defines the GeoJSON filenames. The [MSC Datamart
documentation](https://eccc-msc.github.io/open-data/msc-datamart/readme_en/)
documents the rolling retention window.

### GDSPS storm surge

Geo Stream can also overlay and retrieve ECCC's **Global Deterministic Storm
Surge Prediction System (GDSPS)** — a NEMO-based ocean model that produces
water-level forecasts on a roughly 1/12° global grid. It is offered as a
first-class, optional source alongside CHS water levels and the Coastal
Flooding Risk Index.

**ETAS vs SSH.** GDSPS exposes two distinct variables, and Geo Stream never
substitutes one for the other:

- **ETAS** — storm-surge elevation (metres). The surge component, derived from
  total water level by harmonic analysis.
- **SSH** — total water level / sea-surface height (metres). A modelled total
  water level; it is **not** an engineering or chart datum.

**GeoMet vs Datamart.** Two official ECCC services back the feature:

- **GeoMet WMS** (`https://geo.weather.gc.ca/geomet`) draws the storm-surge
  overlay directly on the interactive map, with the forecast-valid time as the
  WMS `TIME` dimension. The map is the primary visualization — Geo Stream does
  not build a separate Matplotlib or Plotly chart.
- **GeoMet WCS** is the preferred numerical source: a `GetCoverage` request
  returns only the drawn region as NetCDF.
- **MSC Datamart NetCDF** (`https://dd.weather.gc.ca/model_gdsps/`) is the
  guaranteed numerical fallback when WCS advertises no matching coverage.

The exact WMS layer name, WCS coverage id, and Datamart filenames are
**discovered** from the live `GetCapabilities` / directory responses rather
than hardcoded, mirroring how the Coastal Flooding archive discovers its
products. If GeoMet advertises no matching content, Geo Stream says so plainly
and never fabricates an overlay.

**THREDDS/OPeNDAP** is not assumed to exist. A client module is present but
inert: it activates only when `GDSPS_THREDDS_CATALOG_URL` (in
`coastal_flood_explorer/gdsps_thredds.py`) is set to a verified official
catalog. No unconfirmed THREDDS host is shipped.

**Workflow.** Draw an ROI, open the sidebar's **GDSPS Storm Surge** section,
enable the overlay, pick a variable, model run, and forecast-valid time, and
adjust opacity (which never triggers a download). Press **Fetch GDSPS
numerical subset** to retrieve and ROI-mask only the drawn region with Xarray,
then **Download GDSPS export package (ZIP)**.

**Export format.** The ZIP is model-neutral and contains: a NetCDF subset
(`gdsps_subset.nc`), a CSV point time series (`point_time_series.csv`), the ROI
GeoJSON (`roi.geojson`), a metadata JSON (`metadata.json`, including the
variable definition, source service, bounds, and valid times), and a
`README.txt`. No native MIKE, Delft3D, SWAN, or HEC-RAS files are produced.

## How it works

1. On initial load, the app shows recent CHS observations and predictions from
   Bedford Institute so the water-level view is useful before any drawing.
2. In the map's upper-left toolbar, choose the rectangle button and
   click-drag-release, or choose the polygon button, click each corner, and
   click the first point again to finish.
3. The exact drawing automatically selects a CHS observation station inside it,
   or the nearest station when none falls inside. The station selector remains
   available for a deliberate override.
4. To change a region, choose the pencil or trash button, make the change, and
   choose **Save**.
5. Optionally choose an **Archived ECCC issue-date range (UTC)** within the
   latest 30 days and press **Fetch ECCC archive range**. The default includes
   the full rolling window.
6. Geo Stream checks every inclusive daily Datamart directory with visible
   progress, keeps the highest amendment of each product, downloads its static
   GeoJSON files, and combines their features. A date-specific absence is
   reported without discarding other successful dates. A systemic connection,
   rate-limit, or service failure stops the remaining requests instead of
   repeating the same outage across the range. Archive files do not support a
   server-side ROI or bbox.
7. Every source geometry is intersected locally with the exact drawn ROI.
8. Sidebar filters update the map, summary, table, and clipped download without
   another network request.
9. Raw-download actions preserve the decoded CHS time-series documents and the
   per-file ECCC responses and not-loaded date diagnostics before local
   display processing. A partial clipped export says so in its filename and
   includes its loaded-versus-requested date count. The optional forecast
   animation presents one issuance at a time, so forecasts from separate issue
   dates are never combined into one validity frame.

Map navigation stays Canada-focused but includes a buffer around the national
extent so every coast and northern area can be brought fully into view. The
basemap does not wrap around the world. Intentional space below the map lets
the page scroll far enough to centre the map vertically in the browser.

The application keeps its network client, geometry processing, property
normalization, filtering, synthetic generation, and Folium rendering in
separate modules under `coastal_flood_explorer/`. Streamlit session state holds
the current drawing, recent CHS bundles, and the last successful ECCC dataset.
CHS catalogue/time-series results and individual ECCC archive-date outcomes use
bounded caches, including safe failures, so drawing, filtering, overlapping
range requests, and repeated temporary failures do not repeat network requests.
Range-wide cumulative product and feature ceilings stop aggregation before its
in-memory result can grow without bound.

## Installation

Python 3.11 or newer is required. This `C:\Tools` installation uses the shared
virtual environment at `C:\Tools\.venv`:

```text
C:\Tools\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

On Windows, `setup_env.bat` validates that shared environment and installs the
project requirements into it without creating another venv.

For a standalone clone outside `C:\Tools`, create a project-local environment:

```text
python -m venv .venv
```

macOS/Linux:

```text
source .venv/bin/activate
```

Windows PowerShell:

```text
.venv\Scripts\Activate.ps1
```

Install dependencies:

```text
pip install -r requirements.txt
```

## Run locally

```text
streamlit run app.py
```

Windows launchers:

- `run_local.bat` runs Streamlit directly with its native source watcher.
- `run_local_watch.bat` runs a lightweight supervisor that restarts the whole
  Streamlit process after project-file changes or unexpected exits.

Both use `C:\Tools\.venv` and always bind to loopback at
`127.0.0.1:8501`. `GEO_STREAM_PORT` can select another local port.

## Temporary Cloudflare tunnel

First start and verify the local application. Then run:

```text
run_tunnel.bat
```

The script verifies Streamlit's health endpoint, displays a warning, asks for
confirmation, and starts a foreground TryCloudflare quick tunnel. The generated
URL is public, has no application authentication, has no uptime guarantee, and
exists only while `cloudflared` remains running. A longer-lived deployment
should use a named tunnel, a controlled hostname, and Cloudflare Access.
Install and keep `cloudflared` current using
<https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/>.

## Synthetic development mode

Enable **Use synthetic test data** in the sidebar and choose **Generate
synthetic test data** to exercise all risk colours when an archived range is
empty. Synthetic data replaces rather than mixes with archived results and is
labelled on the map, in feature properties, and in the table. It must never be
interpreted as ECCC data.

## Empty-data semantics

Archive dates can contain valid empty files when ECCC published no
coastal-flood-risk polygons for those products. A nonempty range can also have
no polygons intersecting the exact drawn ROI, and filters can reduce
intersected features to zero. A date that failed or was not attempted is
reported as **not loaded**; that is different from a successfully loaded empty
date. None of these cases means that the region is safe, that risk is zero, or
that an all-clear has been issued.

## Tests

```text
python -m pytest
```

Tests mock all HTTP activity and never depend on CHS or ECCC availability.

For an additional syntax check:

```text
python -m compileall app.py coastal_flood_explorer watch_and_run.py
```

## Limitations

- The ECCC collection is experimental and its schema or availability can
  change.
- CHS stations measure water level at points. The selected station may be
  outside a small or inland ROI, which the app reports explicitly.
- Water-level datums and quality flags matter; do not directly compare absolute
  heights from different stations without a documented datum conversion.
- The public raw archive is a rolling window of about 30 days. It is not a
  long-term historical or observed-flood database.
- Folium drawing state is session-local and is not shared between users.
- There is no long-term database, authentication layer, or persistent
  storage.
- OpenStreetMap tiles, CHS IWLS, ECCC Datamart, and quick tunnels require
  internet access.
- A quick tunnel is appropriate only for temporary development and review.
- GDSPS support discovers its WMS layer, WCS coverage, and Datamart filenames
  from live ECCC responses; if ECCC changes or withdraws that content the
  feature reports it as unavailable rather than guessing. GDSPS ETAS is
  storm-surge elevation and SSH is total water level (not a chart/engineering
  datum); the two are never interchanged. The live WMS overlay and real ECCC
  network paths are exercised by hand — the automated test suite mocks all HTTP
  and makes no live network or OPeNDAP calls.

## Possible future architecture

A future operational version could subscribe to ECCC Datamart notifications
through AMQP/Sarracenia, validate and persist incoming products, retain
historical snapshots, and serve a stable API from managed storage. That worker
and storage architecture should remain separate from the interactive
Streamlit process.

## Disclaimer

This application is an exploratory visualization of publicly available CHS and
ECCC data. It is not an official forecast, warning, emergency notification, or
safety determination. Consult official ECCC weather alerts and follow guidance
from local authorities and emergency services.
