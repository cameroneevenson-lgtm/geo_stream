"""CaSR-Land v2.1 HPFX fetch and ROI processing.

CaSR-Land publishes one ~110 MB NetCDF per day for the full North American
domain (rotated-pole grid with 2-D lat/lon). This module downloads that day
file and masks it to the drawn ROI. It is coastal-surface reanalysis, not the
CaSR-Rivers discharge product and not a live flood warning.
"""

from __future__ import annotations

import logging
import os
import tempfile
from datetime import date, datetime
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
    _select_time,
)
from .geometry import GeometryError, parse_roi, roi_bbox

logger = logging.getLogger(__name__)

CASR_LAND_BASE_PATH = "/~scar700/rcas-casr/data/CaSR-Land_v2.1/netcdf/"
CASR_LAND_START = date(1980, 1, 1)
CASR_LAND_END = date(2017, 12, 31)
CASR_LAND_DEFAULT = date(2017, 12, 31)

# Short UI tokens -> NetCDF variable names (verified on a live 2017123112 file).
LAND_AIR_TEMP = "TJ_1.5m"
LAND_DEWPOINT = "TDK_1.5m"
LAND_RUNOFF = "TRAF_Aggregated"
LAND_SWE = "SWE_Land"
LAND_WIND_U = "UDC_10m"
LAND_WIND_V = "VDC_10m"

CASR_LAND_VARIABLES: tuple[str, ...] = (
    LAND_AIR_TEMP,
    LAND_DEWPOINT,
    LAND_RUNOFF,
    LAND_SWE,
    LAND_WIND_U,
    LAND_WIND_V,
)

LAND_NETCDF_NAMES: dict[str, str] = {
    LAND_AIR_TEMP: "CaSR_Land_v2.1_TJ_1.5m",
    LAND_DEWPOINT: "CaSR_Land_v2.1_TDK_1.5m",
    LAND_RUNOFF: "CaSR_Land_v2.1_TRAF_Aggregated",
    LAND_SWE: "CaSR_Land_v2.1_SWE_Land",
    LAND_WIND_U: "CaSR_Land_v2.1_UDC_10m",
    LAND_WIND_V: "CaSR_Land_v2.1_VDC_10m",
}

LAND_VARIABLE_DEFINITIONS: dict[str, str] = {
    LAND_AIR_TEMP: "Air temperature at 1.5 m (deg_C). CaSR-Land reanalysis.",
    LAND_DEWPOINT: "Dew point temperature at 1.5 m (deg_C). CaSR-Land reanalysis.",
    LAND_RUNOFF: (
        "Accumulated surface runoff (kg/m^2). CaSR-Land reanalysis - not a "
        "flood warning."
    ),
    LAND_SWE: "Snow water equivalent on land (kg/m^2). CaSR-Land reanalysis.",
    LAND_WIND_U: "Corrected 10 m U wind, west-east (m/s). CaSR-Land reanalysis.",
    LAND_WIND_V: "Corrected 10 m V wind, south-north (m/s). CaSR-Land reanalysis.",
}

MAX_LAND_FILE_BYTES = 120_000_000
NETCDF_MEDIA_TYPES = frozenset(
    {
        "application/x-netcdf",
        "application/netcdf",
        "image/netcdf",
        "application/octet-stream",
    }
)


def normalize_land_variable(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    for name in CASR_LAND_VARIABLES:
        if cleaned.lower() == name.lower():
            return name
    # Also accept full NetCDF names.
    for short, full in LAND_NETCDF_NAMES.items():
        if cleaned == full:
            return short
    return None


def land_day_url(day: date, *, root: str = CASR_HPFX_ROOT) -> str:
    """Return the HTTPS URL for one CaSR-Land day file (12 UTC stamp)."""

    if not isinstance(day, date):
        raise CASRConfigurationError("A calendar date is required for CaSR-Land.")
    if day < CASR_LAND_START or day > CASR_LAND_END:
        raise CASRConfigurationError(
            "CaSR-Land dates must fall within the published 1980-01 to 2017-12 "
            "window."
        )
    origin = root.rstrip("/")
    return f"{origin}{CASR_LAND_BASE_PATH}{day:%Y%m%d}12.nc"


def download_land_day(
    day: date,
    *,
    session: requests.Session | None = None,
    root: str = CASR_HPFX_ROOT,
) -> bytes:
    """Download one CaSR-Land day NetCDF (~110 MB)."""

    url = land_day_url(day, root=root)
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise CASRConfigurationError("CaSR-Land URLs must use HTTPS.")
    client_session = session if session is not None else requests.Session()
    if session is None:
        _configure_session(client_session)
    root_origin = _origin(root if "://" in root else f"https://{root}")
    if _origin(url) != root_origin:
        raise CASRConfigurationError(
            "CaSR-Land URLs must stay on the configured host."
        )
    try:
        response = client_session.get(
            url,
            timeout=(5.0, 180.0),
            allow_redirects=False,
            stream=True,
        )
    except requests.RequestException as exc:
        logger.warning("CaSR-Land download failed", exc_info=True)
        raise CASRRequestError(
            "CaSR-Land data could not be downloaded from HPFX."
        ) from exc
    if response.status_code == 404:
        raise CASRDataUnavailableError(
            f"No CaSR-Land file is published for {day:%Y-%m-%d}."
        )
    if response.status_code != 200:
        raise CASRRequestError(
            "CaSR-Land data could not be downloaded from HPFX."
        )
    media = _content_type(response)
    if media not in NETCDF_MEDIA_TYPES:
        raise CASRResponseError(
            "The CaSR-Land response was not a NetCDF payload."
        )
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_content(chunk_size=256 * 1024):
        if not chunk:
            continue
        total += len(chunk)
        if total > MAX_LAND_FILE_BYTES:
            raise CASRResponseError(
                "The CaSR-Land NetCDF exceeded the download size limit."
            )
        chunks.append(chunk)
    data = b"".join(chunks)
    if not data:
        raise CASRResponseError("The CaSR-Land NetCDF payload was empty.")
    return data


def process_land_bytes(
    data: bytes,
    *,
    roi: Any,
    variable: str,
    valid_time: datetime | None = None,
    day_label: str = "land",
) -> CASRSubset:
    """Mask one CaSR-Land day file to the ROI and build overlay + series."""

    import xarray as xr

    short = normalize_land_variable(variable)
    if short is None:
        raise CASRConfigurationError(
            "Unsupported CaSR-Land variable."
        )
    var_name = LAND_NETCDF_NAMES[short]
    try:
        geometry = parse_roi(roi)
        roi_box = roi_bbox(geometry)
    except GeometryError as exc:
        raise CASRConfigurationError(str(exc)) from exc
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise CASRResponseError("The CaSR-Land NetCDF payload was empty.")

    handle = tempfile.NamedTemporaryFile(suffix=".nc", delete=False)
    try:
        handle.write(data)
        handle.flush()
        handle.close()
        with xr.open_dataset(handle.name) as dataset:
            return _process_land_dataset(
                dataset,
                geometry=geometry,
                roi_box=roi_box,
                variable=short,
                var_name=var_name,
                valid_time=valid_time,
                day_label=day_label,
            )
    except (CASRConfigurationError, CASRDataUnavailableError, CASRResponseError):
        raise
    except (ValueError, OSError, KeyError) as exc:
        logger.warning("Could not process CaSR-Land NetCDF", exc_info=True)
        raise CASRResponseError(
            "The CaSR-Land NetCDF payload could not be processed."
        ) from exc
    finally:
        try:
            os.unlink(handle.name)
        except OSError:  # pragma: no cover
            pass


def fetch_land_for_roi(
    *,
    day: date,
    variable: str,
    roi: Any,
    download: Any | None = None,
    valid_time: datetime | None = None,
) -> CASRSubset:
    """Download and process one CaSR-Land day for the ROI."""

    fetcher = download if download is not None else download_land_day
    data = fetcher(day)
    return process_land_bytes(
        data,
        roi=roi,
        variable=variable,
        valid_time=valid_time,
        day_label=f"{day:%Y%m%d}",
    )


def _process_land_dataset(
    dataset: Any,
    *,
    geometry: Any,
    roi_box: tuple[float, float, float, float],
    variable: str,
    var_name: str,
    valid_time: datetime | None,
    day_label: str,
) -> CASRSubset:
    if var_name not in dataset.data_vars:
        raise CASRResponseError(
            "The CaSR-Land NetCDF did not contain the expected variable."
        )
    if "lat" not in dataset.coords or "lon" not in dataset.coords:
        raise CASRResponseError(
            "The CaSR-Land NetCDF is missing lat/lon coordinates."
        )
    lat2d = np.asarray(dataset["lat"].values, dtype=float)
    lon2d = np.asarray(dataset["lon"].values, dtype=float)
    if lat2d.ndim != 2 or lon2d.ndim != 2:
        raise CASRResponseError(
            "The CaSR-Land NetCDF lat/lon grid was not two-dimensional."
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
            "The CaSR-Land grid does not intersect the drawn region."
        )

    times = _as_utc_datetimes(dataset["time"].values)
    if not times:
        raise CASRResponseError(
            "The CaSR-Land NetCDF did not contain usable time steps."
        )
    selected = _select_time(times, valid_time)
    time_index = times.index(selected)
    values = np.asarray(
        dataset[var_name].isel(time=time_index).values,
        dtype=float,
    )

    west, south, east, north = roi_box
    in_box = (
        (lon2d >= west)
        & (lon2d <= east)
        & (lat2d >= south)
        & (lat2d <= north)
    )
    if not in_box.any():
        raise CASRDataUnavailableError(
            "No CaSR-Land cells fall inside the drawn region bounding box."
        )
    rows = np.where(in_box.any(axis=1))[0]
    cols = np.where(in_box.any(axis=0))[0]
    r0, r1 = int(rows[0]), int(rows[-1]) + 1
    c0, c1 = int(cols[0]), int(cols[-1]) + 1
    lat_c = lat2d[r0:r1, c0:c1]
    lon_c = lon2d[r0:r1, c0:c1]
    val_c = values[r0:r1, c0:c1]
    mask = shapely.contains_xy(geometry, lon_c, lat_c)
    masked = np.where(mask, val_c, np.nan)
    warnings: tuple[str, ...] = ()
    if not np.isfinite(masked).any():
        masked = np.where(in_box[r0:r1, c0:c1], val_c, np.nan)
        warnings = (
            "No CaSR-Land cells fell inside the exact ROI polygon; cells "
            "inside the ROI bounding box are shown instead.",
        )
    overlay_png = _grid_to_png(masked)
    overlay_bounds = (
        (float(np.nanmin(lat_c)), float(np.nanmin(lon_c))),
        (float(np.nanmax(lat_c)), float(np.nanmax(lon_c))),
    )

    centroid = geometry.centroid
    point_lon, point_lat, series = _land_point_series(
        dataset=dataset,
        var_name=var_name,
        lat2d=lat2d,
        lon2d=lon2d,
        times=times,
        target=(float(centroid.x), float(centroid.y)),
        roi_geometry=geometry,
        row_slice=(r0, r1),
        col_slice=(c0, c1),
    )
    units = dataset[var_name].attrs.get("units")
    if not isinstance(units, str):
        units = None
    return CASRSubset(
        subbasin_id=day_label,
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
        valid_times=tuple(times),
        selected_time=selected,
        warnings=warnings,
    )


def _land_point_series(
    *,
    dataset: Any,
    var_name: str,
    lat2d: np.ndarray,
    lon2d: np.ndarray,
    times: list[datetime],
    target: tuple[float, float],
    roi_geometry: Any,
    row_slice: tuple[int, int],
    col_slice: tuple[int, int],
) -> tuple[float, float, pd.DataFrame]:
    r0, r1 = row_slice
    c0, c1 = col_slice
    lat_c = lat2d[r0:r1, c0:c1]
    lon_c = lon2d[r0:r1, c0:c1]
    sample = np.asarray(
        dataset[var_name].isel(time=0).values[r0:r1, c0:c1],
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
            "The CaSR-Land subset contained no usable values."
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
    series_vals = np.asarray(
        dataset[var_name].isel(rlat=row, rlon=col).values,
        dtype=float,
    )
    frame = pd.DataFrame(
        {
            SERIES_TIME_COLUMN: [
                item.isoformat().replace("+00:00", "Z") for item in times
            ],
            SERIES_VALUE_COLUMN: series_vals,
        }
    )
    return point_lon, point_lat, frame
