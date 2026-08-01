"""CaSR v3.2 access via the PAVICS daily OPeNDAP aggregation.

Streams the Ouranos/PAVICS reformatted daily CaSR v3.2 NcML (CMIP-style
variable names) instead of downloading full-domain HPFX day files. The UI uses
the latest available day only — no date picker — and masks that slice to the
drawn ROI. A short trailing point series is loaded for the chart.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date, datetime
from typing import Any

import numpy as np
import pandas as pd
import shapely

from .casr_common import (
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

CASR_PAVICS_DAILY_URL = (
    "https://pavics.ouranos.ca/twitcher/ows/proxy/thredds/dodsC/"
    "datasets/reanalyses/day_NAM_GovCan_CaSR_v32_1980-2024.ncml"
)

# Compact coastal-relevant subset of the PAVICS daily catalogue.
PAVICS_TAS = "tas"
PAVICS_TASMAX = "tasmax"
PAVICS_TASMIN = "tasmin"
PAVICS_TDPS = "tdps"
PAVICS_PR = "pr"
PAVICS_SNW = "snw"
PAVICS_SFCWIND = "sfcWind"
PAVICS_PSL = "psl"

CASR_PAVICS_VARIABLES: tuple[str, ...] = (
    PAVICS_TAS,
    PAVICS_TASMAX,
    PAVICS_TASMIN,
    PAVICS_TDPS,
    PAVICS_PR,
    PAVICS_SNW,
    PAVICS_SFCWIND,
    PAVICS_PSL,
)

PAVICS_VARIABLE_DEFINITIONS: dict[str, str] = {
    PAVICS_TAS: "Daily mean 1.5 m air temperature (shown in deg_C).",
    PAVICS_TASMAX: "Daily max 1.5 m air temperature (shown in deg_C).",
    PAVICS_TASMIN: "Daily min 1.5 m air temperature (shown in deg_C).",
    PAVICS_TDPS: "Daily mean 1.5 m dew-point temperature (shown in deg_C).",
    PAVICS_PR: "Precipitation flux (kg m-2 s-1). CaSR v3.2 reanalysis.",
    PAVICS_SNW: "Surface snow amount / SWE (kg m-2). CaSR v3.2 reanalysis.",
    PAVICS_SFCWIND: "Near-surface (10 m) wind speed (m s-1).",
    PAVICS_PSL: "Sea-level pressure (Pa). CaSR v3.2 reanalysis.",
}

PAVICS_VARIABLE_LABELS: dict[str, str] = {
    PAVICS_TAS: "tas — 1.5 m air temperature",
    PAVICS_TASMAX: "tasmax — daily max 1.5 m temperature",
    PAVICS_TASMIN: "tasmin — daily min 1.5 m temperature",
    PAVICS_TDPS: "tdps — 1.5 m dew point",
    PAVICS_PR: "pr — precipitation flux",
    PAVICS_SNW: "snw — snow water equivalent",
    PAVICS_SFCWIND: "sfcWind — 10 m wind speed",
    PAVICS_PSL: "psl — sea-level pressure",
}

_TEMPERATURE_VARS = frozenset(
    {
        PAVICS_TAS,
        PAVICS_TASMAX,
        PAVICS_TASMIN,
        PAVICS_TDPS,
    }
)

DEFAULT_SERIES_DAYS = 30
OpenDataset = Callable[..., Any]


def normalize_pavics_variable(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    for name in CASR_PAVICS_VARIABLES:
        if cleaned == name or cleaned.lower() == name.lower():
            return name
    return None


def _default_open_dataset(url: str) -> Any:
    import xarray as xr

    # No dask chunks — Streamlit Cloud may not ship dask, and OPeNDAP slices
    # stay small when we index by time + ROI bbox before .load().
    try:
        return xr.open_dataset(url)
    except Exception as exc:
        logger.warning("PAVICS CaSR open failed", exc_info=True)
        raise CASRRequestError(
            "CaSR v3.2 data could not be opened from PAVICS."
        ) from exc


def latest_available_day(
    *,
    url: str = CASR_PAVICS_DAILY_URL,
    open_dataset: OpenDataset | None = None,
) -> date:
    """Return the newest calendar day published on the PAVICS daily aggregate."""

    opener = open_dataset if open_dataset is not None else _default_open_dataset
    dataset = opener(url)
    try:
        if "time" not in dataset.coords and "time" not in dataset.dims:
            raise CASRResponseError(
                "The PAVICS CaSR dataset did not expose a time axis."
            )
        times = _as_utc_datetimes(dataset["time"].values)
        if not times:
            raise CASRResponseError(
                "The PAVICS CaSR dataset did not contain usable time steps."
            )
        return times[-1].date()
    finally:
        close = getattr(dataset, "close", None)
        if callable(close):
            close()


def fetch_latest_for_roi(
    *,
    roi: Any,
    variable: str,
    url: str = CASR_PAVICS_DAILY_URL,
    series_days: int = DEFAULT_SERIES_DAYS,
    open_dataset: OpenDataset | None = None,
) -> CASRSubset:
    """Fetch the latest PAVICS day for ``variable``, masked to the ROI."""

    short = normalize_pavics_variable(variable)
    if short is None:
        raise CASRConfigurationError("Unsupported CaSR v3.2 variable.")
    if not isinstance(series_days, int) or series_days < 1:
        raise CASRConfigurationError(
            "series_days must be a positive integer."
        )
    try:
        geometry = parse_roi(roi)
        roi_box = roi_bbox(geometry)
    except GeometryError as exc:
        raise CASRConfigurationError(str(exc)) from exc

    opener = open_dataset if open_dataset is not None else _default_open_dataset
    dataset = opener(url)
    try:
        return _process_pavics_dataset(
            dataset,
            geometry=geometry,
            roi_box=roi_box,
            variable=short,
            series_days=series_days,
        )
    except (CASRConfigurationError, CASRDataUnavailableError, CASRResponseError):
        raise
    except Exception as exc:
        logger.warning("PAVICS CaSR processing failed", exc_info=True)
        raise CASRResponseError(
            "The PAVICS CaSR v3.2 response could not be processed."
        ) from exc
    finally:
        close = getattr(dataset, "close", None)
        if callable(close):
            close()


def _process_pavics_dataset(
    dataset: Any,
    *,
    geometry: Any,
    roi_box: tuple[float, float, float, float],
    variable: str,
    series_days: int,
) -> CASRSubset:
    if variable not in dataset.data_vars:
        raise CASRResponseError(
            "The PAVICS CaSR dataset did not contain the expected variable."
        )
    if "lat" not in dataset.coords or "lon" not in dataset.coords:
        raise CASRResponseError(
            "The PAVICS CaSR dataset is missing lat/lon coordinates."
        )
    if "rlat" not in dataset.dims or "rlon" not in dataset.dims:
        raise CASRResponseError(
            "The PAVICS CaSR dataset is missing the rotated-pole grid axes."
        )

    times = _as_utc_datetimes(dataset["time"].values)
    if not times:
        raise CASRResponseError(
            "The PAVICS CaSR dataset did not contain usable time steps."
        )
    selected = times[-1]
    time_index = len(times) - 1
    day_label = selected.strftime("%Y%m%d")

    lat2d = np.asarray(dataset["lat"].values, dtype=float)
    lon2d = np.asarray(dataset["lon"].values, dtype=float)
    if lat2d.ndim != 2 or lon2d.ndim != 2:
        raise CASRResponseError(
            "The PAVICS CaSR lat/lon grid was not two-dimensional."
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

    # Pull only the ROI window for the latest day over OPeNDAP.
    try:
        window = (
            dataset[variable]
            .isel(time=time_index, rlat=slice(r0, r1), rlon=slice(c0, c1))
            .load()
        )
        values = np.asarray(window.values, dtype=float)
    except Exception as exc:
        logger.warning("PAVICS CaSR ROI slice failed", exc_info=True)
        raise CASRRequestError(
            "CaSR v3.2 data could not be fetched from PAVICS for this region."
        ) from exc

    values, units = _maybe_convert_temperature(variable, values, dataset[variable])
    lat_c = lat2d[r0:r1, c0:c1]
    lon_c = lon2d[r0:r1, c0:c1]
    mask = shapely.contains_xy(geometry, lon_c, lat_c)
    masked = np.where(mask, values, np.nan)
    warnings: list[str] = []
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
    point_lon, point_lat, series = _pavics_point_series(
        dataset=dataset,
        variable=variable,
        lat2d=lat2d,
        lon2d=lon2d,
        times=times,
        target=(float(centroid.x), float(centroid.y)),
        roi_geometry=geometry,
        row_slice=(r0, r1),
        col_slice=(c0, c1),
        series_days=series_days,
        convert_temperature=variable in _TEMPERATURE_VARS,
    )
    warnings.append(
        f"Showing the latest published PAVICS day ({selected.date().isoformat()}) "
        f"for the drawn ROI. Date selection is temporarily disabled."
    )
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
        valid_times=(selected,),
        selected_time=selected,
        warnings=tuple(warnings),
    )


def _maybe_convert_temperature(
    variable: str,
    values: np.ndarray,
    data_array: Any,
) -> tuple[np.ndarray, str | None]:
    units = data_array.attrs.get("units")
    if not isinstance(units, str):
        units = None
    if variable not in _TEMPERATURE_VARS:
        return values, units
    if units in {"K", "kelvin", "Kelvin"}:
        return values - 273.15, "deg_C"
    return values, units


def _pavics_point_series(
    *,
    dataset: Any,
    variable: str,
    lat2d: np.ndarray,
    lon2d: np.ndarray,
    times: list[datetime],
    target: tuple[float, float],
    roi_geometry: Any,
    row_slice: tuple[int, int],
    col_slice: tuple[int, int],
    series_days: int,
    convert_temperature: bool,
) -> tuple[float, float, pd.DataFrame]:
    r0, r1 = row_slice
    c0, c1 = col_slice
    lat_c = lat2d[r0:r1, c0:c1]
    lon_c = lon2d[r0:r1, c0:c1]
    # Use the already-selected latest field via a cheap single-time probe for
    # finite/inside masks when possible; fall back to lat/lon geometry alone.
    sample = np.asarray(
        dataset[variable]
        .isel(time=-1, rlat=slice(r0, r1), rlon=slice(c0, c1))
        .values,
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

    start_index = max(0, len(times) - series_days)
    series_times = times[start_index:]
    try:
        series_vals = np.asarray(
            dataset[variable]
            .isel(time=slice(start_index, None), rlat=row, rlon=col)
            .load()
            .values,
            dtype=float,
        )
    except Exception as exc:
        logger.warning("PAVICS CaSR point series failed", exc_info=True)
        raise CASRRequestError(
            "CaSR v3.2 point series could not be fetched from PAVICS."
        ) from exc
    if convert_temperature:
        units = dataset[variable].attrs.get("units")
        if units in {"K", "kelvin", "Kelvin"}:
            series_vals = series_vals - 273.15
    frame = pd.DataFrame(
        {
            SERIES_TIME_COLUMN: [
                item.isoformat().replace("+00:00", "Z") for item in series_times
            ],
            SERIES_VALUE_COLUMN: series_vals,
        }
    )
    return point_lon, point_lat, frame
