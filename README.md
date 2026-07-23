# Geo Stream Coastal Flood Explorer

Geo Stream is an exploratory Streamlit application for viewing Environment and
Climate Change Canada (ECCC) Coastal Flooding Risk Index polygons within a
user-drawn region of interest.

> **Screenshot placeholder:** add an application screenshot here after the
> first visual release.

The application is not an official warning service. Official ECCC weather
alerts and emergency guidance always take precedence.

## Data source

Live data comes from the experimental ECCC GeoMet OGC API collection:

<https://api.weather.gc.ca/collections/coastal_flood_risk_index/items>

Geo Stream does not enumerate ECCC Datamart directories. The current GeoMet
collection is a live view and can validly return no features when no active
coastal-flood products are published.

## How it works

1. In the map's upper-left toolbar, choose the rectangle button and
   click-drag-release, or choose the polygon button, click each corner, and
   click the first point again to finish.
2. To change a region, choose the pencil or trash button, make the change, and
   choose **Save**.
3. Press **Fetch ECCC data**.
4. Geo Stream sends only the ROI bounding box, in CRS84
   `minLon,minLat,maxLon,maxLat` order, to GeoMet.
5. All returned pages are combined and every source geometry is intersected
   locally with the exact drawn ROI.
6. Sidebar filters update the map, summary, table, and download without another
   network request.

Map navigation is constrained to Canada's geographic extent. The basemap does
not wrap or permit zooming out to a worldwide view.

The application keeps its network client, geometry processing, property
normalization, filtering, synthetic generation, and Folium rendering in
separate modules under `coastal_flood_explorer/`. Streamlit session state holds
the current drawing and the last successful dataset; public API responses are
cached for five minutes.

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
synthetic test data** to exercise all risk colours when the live collection is
empty. Synthetic data replaces rather than mixes with live results and is
labelled on the map, in feature properties, and in the table. It must never be
interpreted as ECCC data.

## Empty-data semantics

An empty response means that no active coastal-flooding polygons were returned
for the selected region at fetch time. It does **not** mean that the region is
safe, that risk is zero, or that an all-clear has been issued.

## Tests

```text
pytest
```

Tests mock all HTTP activity and never depend on the live ECCC service.

For an additional syntax check:

```text
python -m compileall app.py coastal_flood_explorer watch_and_run.py
```

## Limitations

- The ECCC collection is experimental and its schema or availability can
  change.
- Folium drawing state is session-local and is not shared between users.
- There is no historical database, authentication layer, or persistent
  storage.
- OpenStreetMap tiles, ECCC GeoMet, and quick tunnels require internet access.
- A quick tunnel is appropriate only for temporary development and review.

## Possible future architecture

A future operational version could subscribe to ECCC Datamart notifications
through AMQP/Sarracenia, validate and persist incoming products, retain
historical snapshots, and serve a stable API from managed storage. That worker
and storage architecture should remain separate from the interactive
Streamlit process.

## Disclaimer

This application is an exploratory visualization of publicly available ECCC
data. It is not an official forecast, warning, emergency notification, or
safety determination. Consult official ECCC weather alerts and follow guidance
from local authorities and emergency services.
