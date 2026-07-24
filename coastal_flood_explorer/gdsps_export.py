"""Model-neutral GDSPS export package builder.

Bundles a fetched GDSPS subset into an in-memory ZIP containing a NetCDF
subset, a CSV point time series, the ROI GeoJSON, a metadata JSON, and a
README.  The package is deliberately model-neutral: no native MIKE, Delft3D,
SWAN, or HEC-RAS files are produced.  ETAS (storm-surge elevation) and SSH
(total water level) are labelled distinctly and never conflated.
"""

from __future__ import annotations

import io
import json
import logging
import os
import tempfile
import zipfile
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from .geometry import sanitize_for_json, serialize_feature_collection
from .gdsps_common import (
    GDSPSConfigurationError,
    GDSPSRun,
    utc_text,
    variable_definition,
)
from .gdsps_processing import GDSPSSubset

logger = logging.getLogger(__name__)

NETCDF_MEMBER = "gdsps_subset.nc"
CSV_MEMBER = "point_time_series.csv"
GEOJSON_MEMBER = "roi.geojson"
METADATA_MEMBER = "metadata.json"
README_MEMBER = "README.txt"
# A fixed member timestamp keeps the ZIP byte-stable for identical inputs.
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def build_export_zip(
    subset: GDSPSSubset,
    *,
    roi: Mapping[str, Any],
    source_service: str,
    run: GDSPSRun | None = None,
    generated_at: datetime | None = None,
) -> bytes:
    """Return a model-neutral GDSPS export package as ZIP bytes."""

    if not isinstance(subset, GDSPSSubset):
        raise GDSPSConfigurationError(
            "A GDSPS subset is required to build the export package."
        )
    if not isinstance(source_service, str) or not source_service.strip():
        raise GDSPSConfigurationError("A source service label is required.")
    generated = generated_at or datetime.now(timezone.utc)

    netcdf_bytes = _dataset_to_bytes(subset.dataset)
    csv_bytes = subset.point_series.to_csv(index=False).encode("utf-8")
    geojson_bytes = _roi_geojson_bytes(roi, subset)
    metadata_bytes = _metadata_bytes(
        subset,
        roi=roi,
        source_service=source_service.strip(),
        run=run,
        generated=generated,
    )
    readme_bytes = _readme_text(subset, source_service.strip()).encode("utf-8")

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in (
            (NETCDF_MEMBER, netcdf_bytes),
            (CSV_MEMBER, csv_bytes),
            (GEOJSON_MEMBER, geojson_bytes),
            (METADATA_MEMBER, metadata_bytes),
            (README_MEMBER, readme_bytes),
        ):
            info = zipfile.ZipInfo(name, date_time=_ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, payload)
    return buffer.getvalue()


def _dataset_to_bytes(dataset: Any) -> bytes:
    handle = tempfile.NamedTemporaryFile(suffix=".nc", delete=False)
    try:
        handle.close()
        dataset.to_netcdf(handle.name)
        with open(handle.name, "rb") as source:
            return source.read()
    except (ValueError, OSError, RuntimeError) as exc:
        logger.warning("Could not serialize GDSPS subset to NetCDF", exc_info=True)
        raise GDSPSConfigurationError(
            "The GDSPS subset could not be written to NetCDF for export."
        ) from exc
    finally:
        try:
            os.unlink(handle.name)
        except OSError:  # pragma: no cover - best-effort cleanup.
            pass


def _roi_geojson_bytes(roi: Mapping[str, Any], subset: GDSPSSubset) -> bytes:
    geometry = _roi_geometry(roi)
    feature = {
        "type": "Feature",
        "geometry": geometry,
        "properties": {
            "role": "region_of_interest",
            "gdsps_variable": subset.variable,
        },
    }
    collection = {"type": "FeatureCollection", "features": [feature]}
    return serialize_feature_collection(collection).encode("utf-8")


def _roi_geometry(roi: Mapping[str, Any]) -> Any:
    if not isinstance(roi, Mapping):
        raise GDSPSConfigurationError("The ROI must be a GeoJSON object.")
    object_type = roi.get("type")
    if object_type == "Feature":
        geometry = roi.get("geometry")
        if not isinstance(geometry, Mapping):
            raise GDSPSConfigurationError("The ROI feature has no geometry.")
        return sanitize_for_json(dict(geometry))
    if object_type in {"Polygon", "MultiPolygon"}:
        return sanitize_for_json(dict(roi))
    raise GDSPSConfigurationError(
        "The ROI must be a Polygon, MultiPolygon, or a Feature containing one."
    )


def _metadata_bytes(
    subset: GDSPSSubset,
    *,
    roi: Mapping[str, Any],
    source_service: str,
    run: GDSPSRun | None,
    generated: datetime,
) -> bytes:
    time_values: list[str] = []
    if subset.time_name is not None and subset.time_name in subset.dataset.coords:
        for value in subset.dataset[subset.time_name].values:
            try:
                time_values.append(utc_text(_as_datetime(value)))
            except Exception:  # noqa: BLE001 - skip an unconvertible stamp.
                continue
    metadata = {
        "product": "Global Deterministic Storm Surge Prediction System (GDSPS)",
        "provider": "Environment and Climate Change Canada (ECCC/MSC)",
        "source_service": source_service,
        "variable": subset.variable,
        "variable_name_in_file": subset.variable_name,
        "variable_definition": variable_definition(subset.variable),
        "units": subset.units,
        "run": run.metadata() if run is not None else None,
        "roi_bbox_crs84": {
            "min_lon": subset.bbox[0],
            "min_lat": subset.bbox[1],
            "max_lon": subset.bbox[2],
            "max_lat": subset.bbox[3],
        },
        "roi_representative_point": {
            "lon": subset.roi_point[0],
            "lat": subset.roi_point[1],
        },
        "forecast_valid_times_utc": time_values,
        "generated_at_utc": utc_text(generated),
        "processing_warnings": list(subset.warnings),
        "notes": (
            "Model-neutral export. ETAS is storm-surge elevation; SSH is total "
            "water level (not an engineering/chart datum). The two are never "
            "interchanged. No native MIKE/Delft3D/SWAN/HEC-RAS files are "
            "included."
        ),
    }
    return json.dumps(
        sanitize_for_json(metadata),
        ensure_ascii=False,
        allow_nan=False,
        indent=2,
    ).encode("utf-8")


def _readme_text(subset: GDSPSSubset, source_service: str) -> str:
    return "\n".join(
        (
            "GDSPS Storm Surge — model-neutral export",
            "=" * 40,
            "",
            "Source: Environment and Climate Change Canada (ECCC/MSC),",
            "Global Deterministic Storm Surge Prediction System (GDSPS).",
            f"Retrieved via: {source_service}.",
            "",
            "Variables",
            "---------",
            "ETAS — storm-surge elevation (metres). Derived from total water",
            "       level (SSH) by harmonic analysis. It is the surge component",
            "       only, not the total water level.",
            "SSH  — total water level / sea-surface height (metres). This is a",
            "       modelled total water level and is NOT an engineering or",
            "       chart datum.",
            "",
            f"This package contains the {subset.variable} variable "
            f"('{subset.variable_name}' in the NetCDF).",
            "ETAS and SSH are never substituted for one another.",
            "",
            "Contents",
            "--------",
            f"{NETCDF_MEMBER}      — ROI-masked NetCDF subset (Xarray/CF).",
            f"{CSV_MEMBER}  — point time series at the ROI representative point.",
            f"{GEOJSON_MEMBER}       — the exact drawn region of interest.",
            f"{METADATA_MEMBER}     — provenance, variable definition, bounds, times.",
            f"{README_MEMBER}        — this file.",
            "",
            "This is a model-neutral export. No native MIKE, Delft3D, SWAN, or",
            "HEC-RAS files are produced.",
            "",
            "Exploratory visualization only. This is not a warning service.",
            "Official ECCC alerts and emergency guidance take precedence.",
        )
    )


def _as_datetime(value: Any) -> datetime:
    import pandas as pd

    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("UTC")
    return stamp.to_pydatetime()
