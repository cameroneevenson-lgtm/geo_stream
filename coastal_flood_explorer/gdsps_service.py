"""Pure GDSPS selection helpers and numerical-fetch orchestration.

This module keeps GDSPS *decision* logic out of ``app.py`` (which is meant to
be Streamlit widgets and cache wiring only).  It performs no I/O or caching
itself: :func:`fetch_numeric` receives the network operations as injected
callables, so the WCS-then-Datamart fallback is unit-testable offline while
``app.py`` supplies its ``st.cache_data``-wrapped fetchers.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from .gdsps_common import (
    GDSPS_VARIABLES,
    GDSPSDataUnavailableError,
    GDSPSCoverageInfo,
    GDSPSDatamartFile,
    GDSPSLayerInfo,
    GDSPSRun,
)
from .gdsps_processing import GDSPSSubset, subset_netcdf_bytes
from .gdsps_wcs import find_coverage_for_variable

# Injected network operations (all ROI/bbox subsetting stays server- or
# client-side; masking happens in gdsps_processing, never here).
CoverageDiscovery = Callable[[], tuple[tuple[GDSPSCoverageInfo, ...], str | None]]
CoverageFetch = Callable[
    [str, tuple[float, float, float, float], datetime | None], bytes
]
FileDiscovery = Callable[[], tuple[tuple[GDSPSDatamartFile, ...], str | None]]
FileFetch = Callable[[str], bytes]


def variable_options(
    layers: tuple[GDSPSLayerInfo, ...],
    files: tuple[GDSPSDatamartFile, ...],
) -> tuple[str, ...]:
    """Return only the GDSPS variables actually discovered upstream."""

    discovered = {layer.variable for layer in layers if layer.variable}
    discovered.update(file.variable for file in files)
    return tuple(
        variable for variable in GDSPS_VARIABLES if variable in discovered
    )


def layer_for_variable(
    layers: tuple[GDSPSLayerInfo, ...],
    variable: str,
) -> GDSPSLayerInfo | None:
    """Return the first discovered WMS layer for a variable, if any."""

    for layer in layers:
        if layer.variable == variable:
            return layer
    return None


def runs_from_files(
    files: tuple[GDSPSDatamartFile, ...],
    variable: str,
) -> tuple[GDSPSRun, ...]:
    """Return the distinct Datamart runs for a variable, newest first."""

    runs = {
        file.run.stamp: file.run
        for file in files
        if file.variable == variable
    }
    return tuple(
        sorted(runs.values(), key=lambda run: run.issue_time, reverse=True)
    )


def valid_times(
    layer: GDSPSLayerInfo | None,
    files: tuple[GDSPSDatamartFile, ...],
    variable: str,
    run: GDSPSRun | None,
) -> tuple[datetime, ...]:
    """Return advertised forecast-valid times, preferring WMS dimensions."""

    if layer is not None and layer.available_times:
        return tuple(layer.available_times)
    times = {
        file.valid_time
        for file in files
        if file.variable == variable
        and (run is None or file.run.stamp == run.stamp)
    }
    return tuple(sorted(times))


def select_datamart_file(
    files: tuple[GDSPSDatamartFile, ...],
    variable: str,
    run: GDSPSRun | None,
    valid_time: datetime | None,
) -> GDSPSDatamartFile:
    """Choose the Datamart file for a variable/run nearest a valid time."""

    candidates = [
        file
        for file in files
        if file.variable == variable
        and (run is None or file.run.stamp == run.stamp)
    ]
    if not candidates:
        raise GDSPSDataUnavailableError(
            "No GDSPS numerical data is available for this selection from "
            "GeoMet WCS or the MSC Datamart."
        )
    if valid_time is not None:
        return min(candidates, key=lambda file: abs(file.valid_time - valid_time))
    return candidates[0]


def fetch_numeric(
    variable: str,
    bbox: tuple[float, float, float, float],
    roi: Mapping[str, Any],
    valid_time: datetime | None,
    run: GDSPSRun | None,
    *,
    wcs_coverages: CoverageDiscovery,
    wcs_bytes: CoverageFetch,
    datamart_files: FileDiscovery,
    datamart_bytes: FileFetch,
) -> tuple[GDSPSSubset, str]:
    """Fetch a ROI-masked subset, preferring WCS then Datamart NetCDF.

    Network operations are injected so this orchestration is offline-testable.
    Returns the processed subset and a human-readable source-service label.
    """

    coverages, _ = wcs_coverages()
    if coverages:
        try:
            coverage = find_coverage_for_variable(coverages, variable)
            data = wcs_bytes(coverage.coverage_id, bbox, valid_time)
            subset = subset_netcdf_bytes(
                data,
                roi=roi,
                variable=variable,
                valid_times=(valid_time,) if valid_time else None,
            )
            return subset, "GeoMet WCS"
        except GDSPSDataUnavailableError:
            # Documented fallback: WCS advertises no coverage for this variable.
            pass

    files, _ = datamart_files()
    chosen = select_datamart_file(files, variable, run, valid_time)
    data = datamart_bytes(chosen.url)
    subset = subset_netcdf_bytes(
        data,
        roi=roi,
        variable=variable,
        valid_times=(chosen.valid_time,),
    )
    return subset, "MSC Datamart"
