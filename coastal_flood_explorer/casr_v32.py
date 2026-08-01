"""Native ECCC CaSR v3.2 via HPFX tiled NetCDF (not PAVICS).

Downloads only the rotated-pole tiles that intersect the drawn ROI from
``…/CaSRv3.2/netcdf_tile/``, for one variable and the period covering the
latest published day. CaSR is a land/atmosphere surface reanalysis: it has
precipitation and snow fields, not coastal water levels, tide gauges, or
storm-surge height.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from collections.abc import Callable, Sequence
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

import numpy as np
import pandas as pd
import requests
import shapely

from .api import _configure_session, _content_type, _origin
from .casr_common import (
    CASR_HPFX_ROOT,
    CASRConfigurationError,
    CASRDataUnavailableError,
    CASRRequestError,
    CASRResponseError,
    bbox_intersects,
    lon_to_wgs84,
)
from .casr_processing import (
    CASRSubset,
    SERIES_TIME_COLUMN,
    SERIES_VALUE_COLUMN,
    _as_utc_datetimes,
    _grid_to_png,
)
from .geometry import GeometryError, parse_roi, roi_bbox

logger = logging.getLogger(__name__)

CASR_V32_TILE_BASE_PATH = "/~scar700/rcas-casr/data/CaSRv3.2/netcdf_tile/"
CASR_V32_DAY_BASE_PATH = "/~scar700/rcas-casr/data/CaSRv3.2/netcdf/"

# Verified against live HPFX day files and tiled 2024 aggregates.
CASR_V32_START = date(1968, 1, 1)
CASR_V32_END = date(2024, 12, 31)
CASR_V32_LATEST = date(2024, 12, 31)

# Rotated-pole grid (0-based). Verified against live tile coordinates.
_RLON0 = -35.397217
_RLAT0 = -44.100002
_STEP = 0.09
_NLON = 706
_NLAT = 778
_TILE = 35
_POLE_LAT = 31.758312454493154
_POLE_LON = 87.59703130293302

# Short UI tokens -> NetCDF variable names on HPFX tiles.
PRECIP_24H = "precip_24h"
PRECIP_HOURLY = "precip_hourly"
RAIN = "rain"
SNOWFALL = "snowfall"
SNOW_PACK = "snow_pack"
SNOW_DEPTH = "snow_depth"
FREEZING_RAIN = "freezing_rain"
ICE_PELLETS = "ice_pellets"

CASR_V32_VARIABLES: tuple[str, ...] = (
    PRECIP_24H,
    PRECIP_HOURLY,
    RAIN,
    SNOWFALL,
    SNOW_PACK,
    SNOW_DEPTH,
    FREEZING_RAIN,
    ICE_PELLETS,
)

V32_NETCDF_NAMES: dict[str, str] = {
    PRECIP_24H: "CaSR_v3.2_A_PR24_SFC",
    PRECIP_HOURLY: "CaSR_v3.2_A_PR0_SFC",
    RAIN: "CaSR_v3.2_P_RN0_SFC",
    SNOWFALL: "CaSR_v3.2_P_SN0_SFC",
    SNOW_PACK: "CaSR_v3.2_P_SWE_LAND",
    SNOW_DEPTH: "CaSR_v3.2_P_SD_LAND",
    FREEZING_RAIN: "CaSR_v3.2_P_FR0_SFC",
    ICE_PELLETS: "CaSR_v3.2_P_PE0_SFC",
}

V32_VARIABLE_LABELS: dict[str, str] = {
    PRECIP_24H: "Precipitation",
    PRECIP_HOURLY: "Hourly precip",
    RAIN: "Rain",
    SNOWFALL: "Snowfall",
    SNOW_PACK: "Snow pack",
    SNOW_DEPTH: "Snow depth",
    FREEZING_RAIN: "Freezing rain",
    ICE_PELLETS: "Ice pellets",
}

V32_VARIABLE_DEFINITIONS: dict[str, str] = {
    PRECIP_24H: (
        "24-hour precipitation analysis (CaPA), as millimetres of water. "
        "Not coastal water level, tide, or storm surge."
    ),
    PRECIP_HOURLY: (
        "Hourly precipitation analysis, as millimetres of water. "
        "Not coastal water level, tide, or storm surge."
    ),
    RAIN: (
        "Model liquid precipitation (mm). Not a gauge water-level series."
    ),
    SNOWFALL: (
        "Model snowfall as liquid-water equivalent (mm). Not water level."
    ),
    SNOW_PACK: (
        "Snow already on the ground as snow water equivalent (mm). "
        "Not coastal water level."
    ),
    SNOW_DEPTH: (
        "Snow depth on land (m). Not coastal water level."
    ),
    FREEZING_RAIN: (
        "Model freezing precipitation as liquid-water equivalent (mm)."
    ),
    ICE_PELLETS: (
        "Model ice pellets as liquid-water equivalent (mm)."
    ),
}

MAX_TILE_BYTES = 80_000_000
MAX_TILES = 6
DEFAULT_SERIES_DAYS = 30
NETCDF_MEDIA_TYPES = frozenset(
    {
        "application/x-netcdf",
        "application/netcdf",
        "image/netcdf",
        "application/octet-stream",
    }
)
_TILE_DIR_RE = re.compile(
    r"^rlon(?P<lon0>\d+)-(?P<lon1>\d+)_rlat(?P<lat0>\d+)-(?P<lat1>\d+)$"
)

DownloadTile = Callable[[str], bytes]
OpenBytes = Callable[[bytes], Any]


def normalize_v32_variable(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    for name in CASR_V32_VARIABLES:
        if cleaned == name or cleaned.lower() == name.lower():
            return name
    for short, full in V32_NETCDF_NAMES.items():
        if cleaned == full:
            return short
    return None


def period_for_day(day: date) -> str:
    """Return the HPFX tiled period token covering ``day``."""

    if day < CASR_V32_START or day > CASR_V32_END:
        raise CASRConfigurationError(
            "CaSR v3.2 dates must fall within the published 1968–2024 window."
        )
    year = day.year
    if year >= 2024:
        return "2024-2024"
    start = 1968 + ((year - 1968) // 4) * 4
    return f"{start}-{start + 3}"


def latest_available_day(
    *,
    session: requests.Session | None = None,
    root: str = CASR_HPFX_ROOT,
) -> date:
    """Return the newest day present on the HPFX full-domain day archive."""

    # Walk backward a few days from the published end; day files are authoritative.
    client = session if session is not None else requests.Session()
    if session is None:
        _configure_session(client)
    for offset in range(0, 14):
        day = CASR_V32_END - timedelta(days=offset)
        url = (
            f"{root.rstrip('/')}{CASR_V32_DAY_BASE_PATH}{day:%Y%m%d}12.nc"
        )
        try:
            response = client.head(
                url,
                timeout=(5.0, 30.0),
                allow_redirects=False,
            )
        except requests.RequestException:
            continue
        if response.status_code == 200:
            return day
    return CASR_V32_LATEST


def fetch_latest_for_roi(
    *,
    roi: Any,
    variable: str,
    day: date | None = None,
    series_days: int = DEFAULT_SERIES_DAYS,
    root: str = CASR_HPFX_ROOT,
    download: DownloadTile | None = None,
) -> CASRSubset:
    """Fetch the latest CaSR v3.2 day for ``variable``, masked to the ROI."""

    short = normalize_v32_variable(variable)
    if short is None:
        raise CASRConfigurationError("Unsupported CaSR v3.2 variable.")
    if not isinstance(series_days, int) or series_days < 1:
        raise CASRConfigurationError("series_days must be a positive integer.")
    try:
        geometry = parse_roi(roi)
        roi_box = roi_bbox(geometry)
    except GeometryError as exc:
        raise CASRConfigurationError(str(exc)) from exc

    selected_day = day if isinstance(day, date) else latest_available_day(root=root)
    if selected_day < CASR_V32_START or selected_day > CASR_V32_END:
        raise CASRConfigurationError(
            "CaSR v3.2 dates must fall within the published 1968–2024 window."
        )
    tiles = tiles_for_bbox(roi_box)
    if not tiles:
        raise CASRDataUnavailableError(
            "No CaSR v3.2 tiles intersect the drawn region."
        )
    if len(tiles) > MAX_TILES:
        raise CASRConfigurationError(
            f"The drawn region covers {len(tiles)} CaSR tiles "
            f"(limit {MAX_TILES}). Draw a smaller region."
        )

    netcdf_name = V32_NETCDF_NAMES[short]
    period = period_for_day(selected_day)
    fetcher = download if download is not None else (
        lambda url: _download_tile_bytes(url, root=root)
    )
    datasets: list[Any] = []
    temp_paths: list[str] = []
    try:
        import xarray as xr

        for tile in tiles:
            url = tile_url(
                netcdf_name,
                tile,
                period,
                root=root,
            )
            payload = fetcher(url)
            handle = tempfile.NamedTemporaryFile(suffix=".nc", delete=False)
            handle.write(payload)
            handle.flush()
            handle.close()
            temp_paths.append(handle.name)
            datasets.append(xr.open_dataset(handle.name))
        if len(datasets) == 1:
            merged = datasets[0]
        else:
            merged = xr.combine_by_coords(
                datasets,
                combine_attrs="override",
            )
        return _process_v32_dataset(
            merged,
            geometry=geometry,
            roi_box=roi_box,
            variable=short,
            netcdf_name=netcdf_name,
            selected_day=selected_day,
            series_days=series_days,
        )
    except (CASRConfigurationError, CASRDataUnavailableError, CASRResponseError):
        raise
    except Exception as exc:
        logger.warning("CaSR v3.2 tile processing failed", exc_info=True)
        raise CASRResponseError(
            "The CaSR v3.2 NetCDF tile(s) could not be processed."
        ) from exc
    finally:
        for dataset in datasets:
            close = getattr(dataset, "close", None)
            if callable(close):
                close()
        for path in temp_paths:
            try:
                os.unlink(path)
            except OSError:  # pragma: no cover
                pass


def tiles_for_bbox(
    bbox: tuple[float, float, float, float],
) -> tuple[str, ...]:
    """Return HPFX tile directory names intersecting a WGS84 bbox."""

    west, south, east, north = bbox
    # Sample corners + centre so a small ROI still maps to tiles.
    samples = (
        (west, south),
        (west, north),
        (east, south),
        (east, north),
        ((west + east) / 2.0, (south + north) / 2.0),
    )
    lon_idx: list[int] = []
    lat_idx: list[int] = []
    for lon, lat in samples:
        rlon, rlat = geo_to_rotated(lon, lat)
        lon_idx.append(_coord_to_index(rlon, _RLON0, _NLON))
        lat_idx.append(_coord_to_index(rlat, _RLAT0, _NLAT))
    i0 = max(1, min(lon_idx))
    i1 = min(_NLON, max(lon_idx))
    j0 = max(1, min(lat_idx))
    j1 = min(_NLAT, max(lat_idx))
    tiles: list[str] = []
    for i in range(_tile_start(i0), i1 + 1, _TILE):
        for j in range(_tile_start(j0), j1 + 1, _TILE):
            tiles.append(_tile_dirname(i, j))
    return tuple(tiles)


def geo_to_rotated(lon: float, lat: float) -> tuple[float, float]:
    """Convert WGS84 lon/lat to CaSR rotated coordinates (degrees)."""

    from pyproj import Transformer

    lon_value = float(lon_to_wgs84(lon))
    if lon_value < 0:
        lon_value += 360.0
    proj = (
        f"+proj=ob_tran +o_proj=longlat +o_lon_p=0 "
        f"+o_lat_p={_POLE_LAT} +lon_0={180.0 + _POLE_LON} +R=6370997"
    )
    transformer = Transformer.from_crs("EPSG:4326", proj, always_xy=True)
    rlon, rlat = transformer.transform(lon_value, float(lat))
    return float(rlon), float(rlat)


def tile_url(
    netcdf_name: str,
    tile: str,
    period: str,
    *,
    root: str = CASR_HPFX_ROOT,
) -> str:
    if _TILE_DIR_RE.match(tile) is None:
        raise CASRConfigurationError("Invalid CaSR v3.2 tile identifier.")
    if not re.fullmatch(r"\d{4}-\d{4}", period):
        raise CASRConfigurationError("Invalid CaSR v3.2 period token.")
    return (
        f"{root.rstrip('/')}{CASR_V32_TILE_BASE_PATH}{tile}/"
        f"{netcdf_name}_{tile}_{period}.nc"
    )


def _tile_start(index_1based: int) -> int:
    return ((index_1based - 1) // _TILE) * _TILE + 1


def _tile_dirname(lon_start: int, lat_start: int) -> str:
    lon_end = min(lon_start + _TILE - 1, _NLON)
    lat_end = min(lat_start + _TILE - 1, _NLAT)
    return f"rlon{lon_start:03d}-{lon_end:03d}_rlat{lat_start:03d}-{lat_end:03d}"


def _coord_to_index(value: float, origin: float, count: int) -> int:
    index = int(round((float(value) - origin) / _STEP)) + 1
    return max(1, min(count, index))


def _download_tile_bytes(
    url: str,
    *,
    root: str = CASR_HPFX_ROOT,
    session: requests.Session | None = None,
) -> bytes:
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise CASRConfigurationError("CaSR v3.2 URLs must use HTTPS.")
    root_origin = _origin(root if "://" in root else f"https://{root}")
    if _origin(url) != root_origin:
        raise CASRConfigurationError(
            "CaSR v3.2 URLs must stay on the configured host."
        )
    client = session if session is not None else requests.Session()
    if session is None:
        _configure_session(client)
    try:
        response = client.get(
            url,
            timeout=(5.0, 180.0),
            allow_redirects=False,
            stream=True,
        )
    except requests.RequestException as exc:
        logger.warning("CaSR v3.2 tile download failed", exc_info=True)
        raise CASRRequestError(
            "CaSR v3.2 data could not be downloaded from HPFX."
        ) from exc
    if response.status_code == 404:
        raise CASRDataUnavailableError(
            "No CaSR v3.2 tile is published for this region/period."
        )
    if response.status_code != 200:
        raise CASRRequestError(
            "CaSR v3.2 data could not be downloaded from HPFX."
        )
    media = _content_type(response)
    if media not in NETCDF_MEDIA_TYPES:
        raise CASRResponseError(
            "The CaSR v3.2 response was not a NetCDF payload."
        )
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_content(chunk_size=256 * 1024):
        if not chunk:
            continue
        total += len(chunk)
        if total > MAX_TILE_BYTES:
            raise CASRResponseError(
                "The CaSR v3.2 NetCDF tile exceeded the download size limit."
            )
        chunks.append(chunk)
    data = b"".join(chunks)
    if not data:
        raise CASRResponseError("The CaSR v3.2 NetCDF payload was empty.")
    return data


def _process_v32_dataset(
    dataset: Any,
    *,
    geometry: Any,
    roi_box: tuple[float, float, float, float],
    variable: str,
    netcdf_name: str,
    selected_day: date,
    series_days: int,
) -> CASRSubset:
    if netcdf_name not in dataset.data_vars:
        raise CASRResponseError(
            "The CaSR v3.2 NetCDF did not contain the expected variable."
        )
    if "lat" not in dataset.coords or "lon" not in dataset.coords:
        raise CASRResponseError(
            "The CaSR v3.2 NetCDF is missing lat/lon coordinates."
        )
    lat2d = np.asarray(dataset["lat"].values, dtype=float)
    lon2d = np.asarray(dataset["lon"].values, dtype=float)
    if lat2d.ndim != 2 or lon2d.ndim != 2:
        raise CASRResponseError(
            "The CaSR v3.2 lat/lon grid was not two-dimensional."
        )
    lon2d = np.vectorize(lon_to_wgs84, otypes=[float])(lon2d)
    grid_box = (
        float(np.nanmin(lon2d)),
        float(np.nanmin(lat2d)),
        float(np.nanmax(lon2d)),
        float(np.nanmax(lat2d)),
    )
    if not bbox_intersects(grid_box, roi_box):
        raise CASRDataUnavailableError(
            "The CaSR v3.2 grid does not intersect the drawn region."
        )

    times = _as_utc_datetimes(dataset["time"].values)
    if not times:
        raise CASRResponseError(
            "The CaSR v3.2 NetCDF did not contain usable time steps."
        )
    # Prefer the last timestep on the selected calendar day; else last overall.
    day_times = [item for item in times if item.date() == selected_day]
    selected = day_times[-1] if day_times else times[-1]
    time_index = times.index(selected)

    west, south, east, north = roi_box
    in_box = (
        (lon2d >= west)
        & (lon2d <= east)
        & (lat2d >= south)
        & (lat2d <= north)
    )
    if not in_box.any():
        raise CASRDataUnavailableError(
            "No CaSR v3.2 cells fall inside the drawn region bounding box."
        )
    rows = np.where(in_box.any(axis=1))[0]
    cols = np.where(in_box.any(axis=0))[0]
    r0, r1 = int(rows[0]), int(rows[-1]) + 1
    c0, c1 = int(cols[0]), int(cols[-1]) + 1

    values = np.asarray(
        dataset[netcdf_name].isel(time=time_index, rlat=slice(r0, r1), rlon=slice(c0, c1)).values,
        dtype=float,
    )
    values, units = _display_units(variable, values, dataset[netcdf_name])
    lat_c = lat2d[r0:r1, c0:c1]
    lon_c = lon2d[r0:r1, c0:c1]
    mask = shapely.contains_xy(geometry, lon_c, lat_c)
    masked = np.where(mask, values, np.nan)
    warnings: list[str] = [
        "CaSR v3.2 is surface weather/hydrometeorology reanalysis — not "
        "coastal water levels, tide gauges, or storm-surge height.",
        f"Showing native HPFX CaSR v3.2 for {selected.date().isoformat()} "
        "(latest published day; date picker disabled).",
    ]
    if not np.isfinite(masked).any():
        masked = np.where(in_box[r0:r1, c0:c1], values, np.nan)
        warnings.append(
            "No CaSR v3.2 cells fell inside the exact ROI polygon; cells "
            "inside the ROI bounding box are shown instead."
        )
    overlay_png = _grid_to_png(masked)
    overlay_bounds = (
        (float(np.nanmin(lat_c)), float(np.nanmin(lon_c))),
        (float(np.nanmax(lat_c)), float(np.nanmax(lon_c))),
    )
    centroid = geometry.centroid
    point_lon, point_lat, series = _point_series(
        dataset=dataset,
        netcdf_name=netcdf_name,
        variable=variable,
        lat2d=lat2d,
        lon2d=lon2d,
        times=times,
        target=(float(centroid.x), float(centroid.y)),
        roi_geometry=geometry,
        row_slice=(r0, r1),
        col_slice=(c0, c1),
        series_days=series_days,
        end_index=time_index,
    )
    return CASRSubset(
        subbasin_id=selected.strftime("%Y%m%d"),
        variable=variable,
        units=units,
        bbox=(
            float(np.nanmin(lon_c)),
            float(np.nanmin(lat_c)),
            float(np.nanmax(lon_c)),
            float(np.nanmax(lat_c)),
        ),
        point=(point_lon, point_lat),
        point_series=series,
        overlay_png=overlay_png,
        overlay_bounds=overlay_bounds,
        valid_times=(selected,),
        selected_time=selected,
        warnings=tuple(warnings),
    )


def _display_units(
    variable: str,
    values: np.ndarray,
    data_array: Any,
) -> tuple[np.ndarray, str | None]:
    units = data_array.attrs.get("units")
    if not isinstance(units, str):
        units = None
    # Precipitation / SWE-like fields often use metres of water.
    if variable in {
        PRECIP_24H,
        PRECIP_HOURLY,
        RAIN,
        SNOWFALL,
        SNOW_PACK,
        FREEZING_RAIN,
        ICE_PELLETS,
    } and units in {"m", "metre", "meter", "metres", "meters"}:
        return values * 1000.0, "mm"
    if variable == SNOW_PACK and units in {"kg m-2", "kg m^-2", "kg/m2"}:
        return values, "mm"
    return values, units


def _point_series(
    *,
    dataset: Any,
    netcdf_name: str,
    variable: str,
    lat2d: np.ndarray,
    lon2d: np.ndarray,
    times: Sequence[datetime],
    target: tuple[float, float],
    roi_geometry: Any,
    row_slice: tuple[int, int],
    col_slice: tuple[int, int],
    series_days: int,
    end_index: int,
) -> tuple[float, float, pd.DataFrame]:
    r0, r1 = row_slice
    c0, c1 = col_slice
    lat_c = lat2d[r0:r1, c0:c1]
    lon_c = lon2d[r0:r1, c0:c1]
    sample = np.asarray(
        dataset[netcdf_name].isel(
            time=end_index,
            rlat=slice(r0, r1),
            rlon=slice(c0, c1),
        ).values,
        dtype=float,
    )
    finite = np.isfinite(sample)
    inside = shapely.contains_xy(roi_geometry, lon_c, lat_c) & finite
    if inside.any():
        candidates_lon = lon_c[inside]
        candidates_lat = lat_c[inside]
        idx_flat = np.flatnonzero(inside.ravel())
    elif finite.any():
        candidates_lon = lon_c[finite]
        candidates_lat = lat_c[finite]
        idx_flat = np.flatnonzero(finite.ravel())
    else:
        raise CASRDataUnavailableError(
            "The CaSR v3.2 subset contained no usable values."
        )
    distance = (candidates_lon - target[0]) ** 2 + (
        candidates_lat - target[1]
    ) ** 2
    pick = int(np.argmin(distance))
    point_lon = float(candidates_lon[pick])
    point_lat = float(candidates_lat[pick])
    local = np.unravel_index(int(idx_flat[pick]), lat_c.shape)
    row = r0 + int(local[0])
    col = c0 + int(local[1])

    end_time = times[end_index]
    if end_time.tzinfo is None:
        end_time = end_time.replace(tzinfo=timezone.utc)
    start_time = end_time - timedelta(days=series_days)
    start_index = 0
    for index, item in enumerate(times):
        if item >= start_time:
            start_index = index
            break
    series_times = list(times[start_index : end_index + 1])
    series_vals = np.asarray(
        dataset[netcdf_name]
        .isel(time=slice(start_index, end_index + 1), rlat=row, rlon=col)
        .values,
        dtype=float,
    )
    series_vals, _ = _display_units(
        variable,
        series_vals,
        dataset[netcdf_name],
    )
    # Hourly series can be huge; keep at most ~daily samples for the chart
    # when more than ~200 points would be drawn.
    if len(series_times) > 200:
        step = max(1, len(series_times) // 120)
        series_times = series_times[::step]
        series_vals = series_vals[::step]
    frame = pd.DataFrame(
        {
            SERIES_TIME_COLUMN: [
                item.isoformat().replace("+00:00", "Z") for item in series_times
            ],
            SERIES_VALUE_COLUMN: series_vals,
        }
    )
    return point_lon, point_lat, frame
