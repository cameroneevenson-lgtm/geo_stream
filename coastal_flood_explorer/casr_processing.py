"""Pure Xarray/Shapely processing for CaSR-Rivers NetCDF.

No network I/O. Opens NetCDF bytes, normalizes longitudes to WGS84, checks or
masks against the ROI, builds a PNG overlay, and extracts a point time series
at the wet cell nearest the ROI centroid (CCCRIS-style node series).
"""

from __future__ import annotations

import io
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd
import shapely

from .casr_common import (
    CASRConfigurationError,
    CASRDataUnavailableError,
    CASRError,
    CASRResponseError,
    bbox_intersects,
    lon_to_wgs84,
    netcdf_variable_name,
    normalize_variable,
)
from .geometry import GeometryError, parse_roi, roi_bbox

logger = logging.getLogger(__name__)

SERIES_TIME_COLUMN = "time_utc"
SERIES_VALUE_COLUMN = "value"


@dataclass(frozen=True, slots=True)
class CASRSubset:
    """ROI-relevant CaSR-Rivers subset for one sub-basin."""

    subbasin_id: str
    variable: str
    units: str | None
    bbox: tuple[float, float, float, float]
    point: tuple[float, float]
    point_series: pd.DataFrame
    overlay_png: bytes
    overlay_bounds: tuple[tuple[float, float], tuple[float, float]]
    valid_times: tuple[datetime, ...]
    selected_time: datetime
    warnings: tuple[str, ...] = ()


def grid_bbox_from_bytes(data: bytes) -> tuple[float, float, float, float]:
    """Return WGS84 ``(min_lon, min_lat, max_lon, max_lat)`` from NetCDF bytes."""

    import xarray as xr

    if not isinstance(data, (bytes, bytearray)) or not data:
        raise CASRResponseError("The CaSR-Rivers NetCDF payload was empty.")
    handle = tempfile.NamedTemporaryFile(suffix=".nc", delete=False)
    try:
        handle.write(data)
        handle.flush()
        handle.close()
        with xr.open_dataset(handle.name) as dataset:
            return _dataset_bbox(dataset)
    except (ValueError, OSError, KeyError) as exc:
        logger.warning("Could not read CaSR grid bbox", exc_info=True)
        raise CASRResponseError(
            "The CaSR-Rivers NetCDF coordinates could not be read."
        ) from exc
    finally:
        try:
            os.unlink(handle.name)
        except OSError:  # pragma: no cover
            pass


def subset_intersects_roi(data: bytes, roi: Any) -> bool:
    """Return whether the NetCDF grid bbox intersects the ROI bbox."""

    try:
        geometry = parse_roi(roi)
        roi_box = roi_bbox(geometry)
    except GeometryError as exc:
        raise CASRConfigurationError(str(exc)) from exc
    return bbox_intersects(grid_bbox_from_bytes(data), roi_box)


def process_rivers_bytes(
    data: bytes,
    *,
    roi: Any,
    variable: str,
    subbasin_id: str,
    valid_time: datetime | None = None,
) -> CASRSubset:
    """Open NetCDF bytes and build overlay + series for one sub-basin."""

    import xarray as xr

    canonical = normalize_variable(variable)
    if canonical is None:
        raise CASRConfigurationError(
            "CaSR-Rivers variable must be RiverDischarge, "
            "RiverChannelStorage, or DeepReservoirStorage."
        )
    try:
        geometry = parse_roi(roi)
        roi_box = roi_bbox(geometry)
    except GeometryError as exc:
        raise CASRConfigurationError(str(exc)) from exc

    if not isinstance(data, (bytes, bytearray)) or not data:
        raise CASRResponseError("The CaSR-Rivers NetCDF payload was empty.")

    handle = tempfile.NamedTemporaryFile(suffix=".nc", delete=False)
    try:
        handle.write(data)
        handle.flush()
        handle.close()
        with xr.open_dataset(handle.name) as dataset:
            return _process_dataset(
                dataset,
                geometry=geometry,
                roi_box=roi_box,
                variable=canonical,
                subbasin_id=subbasin_id,
                valid_time=valid_time,
            )
    except CASRError:
        raise
    except (ValueError, OSError, KeyError) as exc:
        logger.warning("Could not process CaSR NetCDF", exc_info=True)
        raise CASRResponseError(
            "The CaSR-Rivers NetCDF payload could not be processed."
        ) from exc
    finally:
        try:
            os.unlink(handle.name)
        except OSError:  # pragma: no cover
            pass


def _process_dataset(
    dataset: Any,
    *,
    geometry: Any,
    roi_box: tuple[float, float, float, float],
    variable: str,
    subbasin_id: str,
    valid_time: datetime | None,
) -> CASRSubset:
    var_name = netcdf_variable_name(variable)
    if var_name not in dataset.data_vars:
        raise CASRResponseError(
            "The CaSR-Rivers NetCDF did not contain the expected variable."
        )
    data_var = dataset[var_name]
    if "lat" not in dataset.coords or "lon" not in dataset.coords:
        raise CASRResponseError(
            "The CaSR-Rivers NetCDF is missing lat/lon coordinates."
        )

    lats = np.asarray(dataset["lat"].values, dtype=float)
    lons = np.asarray(
        [lon_to_wgs84(float(v)) for v in dataset["lon"].values],
        dtype=float,
    )
    grid_box = (
        float(np.nanmin(lons)),
        float(np.nanmin(lats)),
        float(np.nanmax(lons)),
        float(np.nanmax(lats)),
    )
    if not bbox_intersects(grid_box, roi_box):
        raise CASRDataUnavailableError(
            "The CaSR-Rivers sub-basin grid does not intersect the drawn region."
        )

    if "time" not in dataset.coords:
        raise CASRResponseError(
            "The CaSR-Rivers NetCDF is missing a time coordinate."
        )
    times = _as_utc_datetimes(dataset["time"].values)
    if not times:
        raise CASRResponseError(
            "The CaSR-Rivers NetCDF did not contain usable time steps."
        )
    selected = _select_time(times, valid_time)
    time_index = times.index(selected)

    values = np.asarray(data_var.isel(time=time_index).values, dtype=float)
    lon2d, lat2d = np.meshgrid(lons, lats)
    # Mask cells outside the exact ROI polygon.
    mask = shapely.contains_xy(geometry, lon2d, lat2d)
    masked = np.where(mask, values, np.nan)
    if not np.isfinite(masked).any():
        # Fall back to full grid when the polygon is tiny vs the coarse cell.
        masked = values
        warnings: tuple[str, ...] = (
            "No CaSR grid cells fell inside the exact ROI polygon; the "
            "sub-basin grid intersecting the ROI bounding box is shown instead.",
        )
    else:
        warnings = ()

    overlay_png = _grid_to_png(masked)
    overlay_bounds = (
        (float(np.nanmin(lats)), float(np.nanmin(lons))),
        (float(np.nanmax(lats)), float(np.nanmax(lons))),
    )

    centroid = geometry.centroid
    point_lon, point_lat, series = _point_series(
        data_var=data_var,
        lons=lons,
        lats=lats,
        times=times,
        target=(float(centroid.x), float(centroid.y)),
        roi_geometry=geometry,
    )

    units = data_var.attrs.get("units")
    if not isinstance(units, str):
        units = None

    return CASRSubset(
        subbasin_id=subbasin_id,
        variable=variable,
        units=units,
        bbox=grid_box,
        point=(point_lon, point_lat),
        point_series=series,
        overlay_png=overlay_png,
        overlay_bounds=overlay_bounds,
        valid_times=tuple(times),
        selected_time=selected,
        warnings=warnings,
    )


def _dataset_bbox(dataset: Any) -> tuple[float, float, float, float]:
    lats = np.asarray(dataset["lat"].values, dtype=float)
    lons = np.asarray(
        [lon_to_wgs84(float(v)) for v in dataset["lon"].values],
        dtype=float,
    )
    return (
        float(np.nanmin(lons)),
        float(np.nanmin(lats)),
        float(np.nanmax(lons)),
        float(np.nanmax(lats)),
    )


def _as_utc_datetimes(values: Any) -> list[datetime]:
    index = pd.to_datetime(values, utc=True)
    out: list[datetime] = []
    for item in index.to_pydatetime():
        if item.tzinfo is None:
            item = item.replace(tzinfo=timezone.utc)
        else:
            item = item.astimezone(timezone.utc)
        out.append(item)
    return out


def _select_time(
    times: list[datetime],
    wanted: datetime | None,
) -> datetime:
    if wanted is None:
        return times[len(times) // 2]
    if wanted.tzinfo is None:
        wanted = wanted.replace(tzinfo=timezone.utc)
    else:
        wanted = wanted.astimezone(timezone.utc)
    if wanted in times:
        return wanted
    return min(times, key=lambda item: abs((item - wanted).total_seconds()))


def _point_series(
    *,
    data_var: Any,
    lons: np.ndarray,
    lats: np.ndarray,
    times: list[datetime],
    target: tuple[float, float],
    roi_geometry: Any,
) -> tuple[float, float, pd.DataFrame]:
    lon2d, lat2d = np.meshgrid(lons, lats)
    # Prefer wet cells inside the ROI; else nearest wet cell on the grid.
    sample = np.asarray(data_var.isel(time=0).values, dtype=float)
    finite = np.isfinite(sample)
    inside = shapely.contains_xy(roi_geometry, lon2d, lat2d) & finite
    if inside.any():
        candidates_lon = lon2d[inside]
        candidates_lat = lat2d[inside]
    elif finite.any():
        candidates_lon = lon2d[finite]
        candidates_lat = lat2d[finite]
    else:
        raise CASRDataUnavailableError(
            "The CaSR-Rivers sub-basin grid contained no usable values."
        )
    distance = (candidates_lon - target[0]) ** 2 + (
        candidates_lat - target[1]
    ) ** 2
    pick = int(np.argmin(distance))
    point_lon = float(candidates_lon[pick])
    point_lat = float(candidates_lat[pick])
    # Locate indices on the full grid.
    lon_idx = int(np.argmin(np.abs(lons - point_lon)))
    lat_idx = int(np.argmin(np.abs(lats - point_lat)))
    series_vals = np.asarray(
        data_var.isel(lat=lat_idx, lon=lon_idx).values,
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


def _grid_to_png(values: np.ndarray) -> bytes:
    """Render a float grid to an RGBA PNG (viridis-like, transparent NaNs)."""

    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover
        raise CASRResponseError(
            "Pillow is required to render CaSR map overlays."
        ) from exc

    finite = np.isfinite(values)
    if not finite.any():
        raise CASRDataUnavailableError(
            "The CaSR-Rivers grid contained no finite values to display."
        )
    vmin = float(np.nanpercentile(values[finite], 5))
    vmax = float(np.nanpercentile(values[finite], 95))
    if not np.isfinite(vmin) or not np.isfinite(vmax) or vmax <= vmin:
        vmin = float(np.nanmin(values))
        vmax = float(np.nanmax(values))
        if not np.isfinite(vmin) or not np.isfinite(vmax) or vmax <= vmin:
            vmax = vmin + 1.0
    scaled = (values - vmin) / (vmax - vmin)
    scaled = np.clip(scaled, 0.0, 1.0)
    rgba = _viridis_rgba(scaled)
    rgba[~finite] = (0, 0, 0, 0)
    image = Image.fromarray(rgba, mode="RGBA")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _viridis_rgba(scaled: np.ndarray) -> np.ndarray:
    """Approximate viridis without requiring matplotlib."""

    # Control stops roughly matching viridis.
    stops = np.array(
        [
            [68, 1, 84],
            [59, 82, 139],
            [33, 145, 140],
            [94, 201, 98],
            [253, 231, 37],
        ],
        dtype=float,
    )
    positions = np.linspace(0.0, 1.0, len(stops))
    flat = scaled.ravel()
    channels = [
        np.interp(flat, positions, stops[:, channel])
        for channel in range(3)
    ]
    rgb = np.stack(channels, axis=1).reshape(scaled.shape + (3,))
    alpha = np.full(scaled.shape + (1,), 220.0)
    return np.concatenate([rgb, alpha], axis=-1).astype(np.uint8)
