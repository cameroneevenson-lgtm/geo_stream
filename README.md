# Geo Stream Coastal Flood Explorer

Geo Stream is an exploratory Streamlit application for viewing Environment and
Climate Change Canada (ECCC) Coastal Flooding Risk Index polygons within a
user-drawn region of interest.

> **Screenshot placeholder:** add an application screenshot here after the
> first visual release.

The application is not an official warning service. Official ECCC weather
alerts and emergency guidance always take precedence.

## Data source

The application uses ECCC's official rolling MSC Datamart forecast archive:

<https://dd.weather.gc.ca/YYYYMMDD/WXO-DD/coastal-flooding/risk-index/>

ECCC currently retains about 30 days of Datamart files. These are archived
forecasts, not observed flood events, a 30-day average, inundation maps, or a
permanent hazard layer. The current GeoMet collection still exists, but its
current-publication view can be globally empty; Geo Stream therefore asks for
an explicit recent archive issue date instead.

The [Coastal Flooding Risk Index Datamart
documentation](https://eccc-msc.github.io/open-data/msc-data/coastal-flooding/readme_coastal-flooding-risk-index-datamart_en/)
defines the GeoJSON filenames. The [MSC Datamart
documentation](https://eccc-msc.github.io/open-data/msc-datamart/readme_en/)
documents the rolling retention window.

## How it works

1. In the map's upper-left toolbar, choose the rectangle button and
   click-drag-release, or choose the polygon button, click each corner, and
   click the first point again to finish.
2. To change a region, choose the pencil or trash button, make the change, and
   choose **Save**.
3. Choose an **Archived ECCC issue date (UTC)** from the latest 30 days and
   press **Fetch archived ECCC forecast**.
4. Geo Stream lists that official Datamart directory, keeps the highest
   amendment of each product, downloads its static GeoJSON files, and combines
   their features. Archive files do not support a server-side ROI or bbox.
5. Every source geometry is intersected locally with the exact drawn ROI.
6. Sidebar filters update the map, summary, table, and clipped download without
   another network request.
7. **Download raw fetched ECCC JSON** supplies the per-file responses before
   clipping or filtering. When at least two validity times intersect the ROI,
   the optional timeline animates those forecast frames within the loaded
   issue; it does not fetch or average all 30 retained days.

Map navigation stays Canada-focused but includes a buffer around the national
extent so every coast and northern area can be brought fully into view. The
basemap does not wrap around the world.

The application keeps its network client, geometry processing, property
normalization, filtering, synthetic generation, and Folium rendering in
separate modules under `coastal_flood_explorer/`. Streamlit session state holds
the current drawing and the last successful dataset; archive results are cached
for five minutes by official archive root and UTC issue date.

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
synthetic test data** to exercise all risk colours when an archived issue is
empty. Synthetic data replaces rather than mixes with archived results and is
labelled on the map, in feature properties, and in the table. It must never be
interpreted as ECCC data.

## Empty-data semantics

An archive issue can contain valid empty files when ECCC published no
coastal-flood-risk polygons for those products. A nonempty issue can also have
no polygons intersecting the exact drawn ROI, and filters can reduce intersected
features to zero. None of these cases means that the region is safe, that risk
is zero, or that an all-clear has been issued.

## Tests

```text
pytest
```

Tests mock all HTTP activity and never depend on the ECCC service.

For an additional syntax check:

```text
python -m compileall app.py coastal_flood_explorer watch_and_run.py
```

## Limitations

- The ECCC collection is experimental and its schema or availability can
  change.
- The public raw archive is a rolling window of about 30 days. It is not a
  long-term historical or observed-flood database.
- Folium drawing state is session-local and is not shared between users.
- There is no long-term database, authentication layer, or persistent
  storage.
- OpenStreetMap tiles, ECCC Datamart, and quick tunnels require internet access.
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
