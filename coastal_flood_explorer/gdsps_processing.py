"""Pure Xarray/Shapely processing for GDSPS NetCDF subsets.

This module never performs network I/O.  It opens NetCDF bytes (or accepts an
already-open lazy dataset from OPeNDAP), prefilters to the ROI bounding box
*before* loading, masks exactly to the ROI polygon, selects only the requested
forecast-valid time(s), and extracts a point time series.  The full global grid
is never loaded — bbox selection happens on the lazy dataset first.

ETAS (storm-surge elevation) and SSH (total water level) are resolved
independently and never substituted for one another.
"""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

import shapely

from .geometry import GeometryError, parse_roi, roi_bbox
from .gdsps_common import (
    GDSPSConfigurationError,
    GDSPSDataUnavailableError,
    GDSPSResponseError,
    classify_variable,
    normalize_variable,
)

logger = logging.getLogger(__name__)

_LON_NAMES = ("longitude", "lon", "nav_lon", "x", "long")
_LAT_NAMES = ("latitude", "lat", "nav_lat", "y")
_TIME_NAMES = ("time", "forecast_time", "valid_time", "leadtime")
_BBOX_MARGIN_DEG = 0.5


@dataclass(frozen=True, slots=True)
class GDSPSSubset:
    """A ROI-masked GDSPS subset plus a point time series and metadata."""

    dataset: Any
    point_series: pd.DataFrame
    variable: str
    variable_name: str
    longitude_name: str
    latitude_name: str
    time_name: str | None
    bbox: tuple[float, float, float, float]
    roi_point: tuple[float, float]
    units: str | None
    warnings: tuple[str, ...] = ()


def subset_netcdf_bytes(
    data: bytes,
    *,
    roi: Any,
    variable: str,
    valid_times: tuple[datetime, ...] | None = None,
) -> GDSPSSubset:
    """Open NetCDF bytes and return a ROI-masked subset for one variable."""

    import xarray as xr

    if not isinstance(data, (bytes, bytearray)) or not data:
        raise GDSPSResponseError("The GDSPS NetCDF payload was empty.")
    handle = tempfile.NamedTemporaryFile(
        suffix=".nc", delete=False
    )
    try:
        handle.write(data)
        handle.flush()
        handle.close()
        with xr.open_dataset(handle.name) as dataset:
            return process_dataset(
                dataset,
                roi=roi,
                variable=variable,
                valid_times=valid_times,
            )
    except (ValueError, OSError) as exc:
        logger.warning("Could not open GDSPS NetCDF payload", exc_info=True)
        raise GDSPSResponseError(
            "The GDSPS NetCDF payload could not be opened."
        ) from exc
    finally:
        try:
            os.unlink(handle.name)
        except OSError:  # pragma: no cover - best-effort cleanup.
            pass


def process_dataset(
    dataset: Any,
    *,
    roi: Any,
    variable: str,
    valid_times: tuple[datetime, ...] | None = None,
) -> GDSPSSubset:
    """Subset, mask, and extract a point series from an open dataset.

    The subset is loaded into memory before returning so callers may safely
    close the source dataset (or its OPeNDAP connection).
    """

    target_variable = normalize_variable(variable)
    if target_variable is None:
        raise GDSPSConfigurationError(
            "The GDSPS variable must be ETAS or SSH."
        )
    try:
        roi_geometry = parse_roi(roi)
        min_lon, min_lat, max_lon, max_lat = roi_bbox(roi)
    except GeometryError as exc:
        raise GDSPSConfigurationError(str(exc)) from exc

    lon_name = _find_coordinate(dataset, _LON_NAMES)
    lat_name = _find_coordinate(dataset, _LAT_NAMES)
    if lon_name is None or lat_name is None:
        raise GDSPSDataUnavailableError(
            "The GDSPS dataset does not expose recognizable longitude and "
            "latitude coordinates."
        )
    time_name = _find_coordinate(dataset, _TIME_NAMES)
    variable_name, data_array = _select_variable(
        dataset, target_variable, (lon_name, lat_name)
    )

    warnings: list[str] = []
    subset = _bbox_prefilter(
        data_array,
        lon_name=lon_name,
        lat_name=lat_name,
        bounds=(min_lon, min_lat, max_lon, max_lat),
        warnings=warnings,
    )
    if time_name is not None and valid_times:
        subset = _select_times(subset, time_name, valid_times, warnings)

    lon_values = np.asarray(subset[lon_name].values, dtype=float)
    lat_values = np.asarray(subset[lat_name].values, dtype=float)
    if lon_values.size == 0 or lat_values.size == 0:
        raise GDSPSDataUnavailableError(
            "No GDSPS grid cells fall within the drawn region."
        )

    masked = _apply_roi_mask(
        subset,
        roi_geometry=roi_geometry,
        lon_name=lon_name,
        lat_name=lat_name,
        lon_values=lon_values,
        lat_values=lat_values,
    )

    roi_point = roi_geometry.representative_point()
    point_series = _point_time_series(
        masked,
        lon_name=lon_name,
        lat_name=lat_name,
        time_name=time_name,
        point_lon=float(roi_point.x),
        point_lat=float(roi_point.y),
        variable=target_variable,
    )

    result = masked.to_dataset(name=variable_name)
    result = result.load()
    units = data_array.attrs.get("units")
    return GDSPSSubset(
        dataset=result,
        point_series=point_series,
        variable=target_variable,
        variable_name=variable_name,
        longitude_name=lon_name,
        latitude_name=lat_name,
        time_name=time_name,
        bbox=(min_lon, min_lat, max_lon, max_lat),
        roi_point=(float(roi_point.x), float(roi_point.y)),
        units=str(units) if units is not None else None,
        warnings=tuple(warnings),
    )


def _find_coordinate(dataset: Any, candidates: tuple[str, ...]) -> str | None:
    lowered = {str(name).lower(): str(name) for name in dataset.variables}
    for candidate in candidates:
        if candidate in lowered:
            return lowered[candidate]
    # Fall back to CF ``standard_name`` attributes.
    for name in dataset.variables:
        attrs = dataset[name].attrs
        standard = str(attrs.get("standard_name", "")).lower()
        if standard in candidates:
            return str(name)
    return None


def _select_variable(
    dataset: Any,
    variable: str,
    coordinate_names: tuple[str, ...],
) -> tuple[str, Any]:
    data_vars = [
        str(name)
        for name in dataset.data_vars
        if str(name) not in coordinate_names
    ]
    # 1) Exact code match wins.
    for name in data_vars:
        if normalize_variable(name) == variable:
            return name, dataset[name]
    # 2) Otherwise classify by name/standard_name/long_name.
    for name in data_vars:
        attrs = dataset[name].attrs
        classified = classify_variable(
            name,
            str(attrs.get("standard_name", "")),
            str(attrs.get("long_name", "")),
        )
        if classified == variable:
            return name, dataset[name]
    raise GDSPSDataUnavailableError(
        f"The GDSPS dataset does not contain a {variable} variable."
    )


def _bbox_prefilter(
    data_array: Any,
    *,
    lon_name: str,
    lat_name: str,
    bounds: tuple[float, float, float, float],
    warnings: list[str],
) -> Any:
    min_lon, min_lat, max_lon, max_lat = bounds
    lon_coord = data_array[lon_name]
    lat_coord = data_array[lat_name]
    if lon_coord.ndim != 1 or lat_coord.ndim != 1:
        warnings.append(
            "The GDSPS grid uses multi-dimensional coordinates; the exact ROI "
            "mask is applied without a bounding-box pre-slice."
        )
        return data_array

    lon_dim = lon_coord.dims[0]
    lat_dim = lat_coord.dims[0]
    lon_low, lon_high = _bbox_longitudes(
        lon_coord.values, min_lon, max_lon
    )
    lon_selection = _coordinate_slice(
        lon_coord.values, lon_low - _BBOX_MARGIN_DEG, lon_high + _BBOX_MARGIN_DEG
    )
    lat_selection = _coordinate_slice(
        lat_coord.values, min_lat - _BBOX_MARGIN_DEG, max_lat + _BBOX_MARGIN_DEG
    )
    return data_array.isel({lon_dim: lon_selection, lat_dim: lat_selection})


def _bbox_longitudes(
    lon_values: np.ndarray,
    min_lon: float,
    max_lon: float,
) -> tuple[float, float]:
    values = np.asarray(lon_values, dtype=float)
    if values.size and float(np.nanmax(values)) > 180.0:
        # Grid uses a 0..360 convention; shift the requested western bounds.
        return (min_lon % 360.0, max_lon % 360.0)
    return (min_lon, max_lon)


def _coordinate_slice(
    values: np.ndarray,
    low: float,
    high: float,
) -> np.ndarray:
    array = np.asarray(values, dtype=float)
    mask = (array >= low) & (array <= high)
    indices = np.nonzero(mask)[0]
    if indices.size == 0:
        # Keep the single nearest index so downstream masking can report an
        # empty ROI intersection rather than crash on a zero-length axis.
        nearest = int(np.argmin(np.abs(array - (low + high) / 2.0)))
        return np.array([nearest], dtype=int)
    return np.arange(int(indices.min()), int(indices.max()) + 1, dtype=int)


def _select_times(
    data_array: Any,
    time_name: str,
    valid_times: tuple[datetime, ...],
    warnings: list[str],
) -> Any:
    requested = pd.to_datetime(
        [pd.Timestamp(value) for value in valid_times], utc=True
    ).tz_localize(None)
    available = pd.to_datetime(data_array[time_name].values)
    if getattr(available, "tz", None) is not None:
        available = available.tz_convert("UTC").tz_localize(None)
    selected_positions = sorted(
        {int(np.argmin(np.abs(available - moment))) for moment in requested}
    )
    if not selected_positions:
        warnings.append("No requested forecast-valid time matched the dataset.")
        return data_array
    return data_array.isel({time_name: selected_positions})


def _apply_roi_mask(
    data_array: Any,
    *,
    roi_geometry: Any,
    lon_name: str,
    lat_name: str,
    lon_values: np.ndarray,
    lat_values: np.ndarray,
) -> Any:
    if data_array[lon_name].ndim == 1 and data_array[lat_name].ndim == 1:
        lon_grid, lat_grid = np.meshgrid(lon_values, lat_values)
    else:
        lon_grid = np.asarray(data_array[lon_name].values, dtype=float)
        lat_grid = np.asarray(data_array[lat_name].values, dtype=float)
    # shapely.vectorized expects lon (x), lat (y); wrap 0..360 grids back to
    # signed longitudes for the polygon test.
    lon_for_test = np.where(lon_grid > 180.0, lon_grid - 360.0, lon_grid)
    inside = np.asarray(
        shapely.contains_xy(roi_geometry, lon_for_test, lat_grid), dtype=bool
    )
    if not inside.any():
        raise GDSPSDataUnavailableError(
            "No GDSPS grid cells fall within the drawn region."
        )
    lat_dim = data_array[lat_name].dims[0]
    lon_dim = data_array[lon_name].dims[0]
    import xarray as xr

    mask = xr.DataArray(inside, dims=(lat_dim, lon_dim))
    return data_array.where(mask)


def _point_time_series(
    data_array: Any,
    *,
    lon_name: str,
    lat_name: str,
    time_name: str | None,
    point_lon: float,
    point_lat: float,
    variable: str,
) -> pd.DataFrame:
    lon_values = np.asarray(data_array[lon_name].values, dtype=float)
    lat_values = np.asarray(data_array[lat_name].values, dtype=float)
    lon_test = np.where(lon_values > 180.0, lon_values - 360.0, lon_values)
    lon_index = int(np.argmin(np.abs(lon_test - point_lon)))
    lat_index = int(np.argmin(np.abs(lat_values - point_lat)))
    lon_dim = data_array[lon_name].dims[0]
    lat_dim = data_array[lat_name].dims[0]
    point = data_array.isel({lon_dim: lon_index, lat_dim: lat_index})

    column = f"{variable}_value"
    if time_name is not None and time_name in point.dims:
        times = pd.to_datetime(point[time_name].values, utc=True)
        values = np.asarray(point.values, dtype=float).reshape(-1)
        frame = pd.DataFrame(
            {"time_utc": times, column: values}
        )
    else:
        value = float(np.asarray(point.values, dtype=float).reshape(-1)[0])
        frame = pd.DataFrame({"time_utc": [pd.NaT], column: [value]})
    return frame
