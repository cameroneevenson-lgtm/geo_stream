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
    GDSPS_MODEL,
    GDSPS_VARIABLES,
    SURGE_MODELS,
    GDSPSDataUnavailableError,
    GDSPSCoverageInfo,
    GDSPSDatamartFile,
    GDSPSLayerInfo,
    GDSPSRequestError,
    GDSPSResponseError,
    GDSPSRun,
    bbox_intersects,
)
from .gdsps_processing import GDSPSSubset, subset_netcdf_bytes
from .gdsps_wcs import find_coverage

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
    """Return only the storm-surge variables actually discovered upstream."""

    discovered = {layer.variable for layer in layers if layer.variable}
    discovered.update(file.variable for file in files)
    return tuple(
        variable for variable in GDSPS_VARIABLES if variable in discovered
    )


def models_available(
    layers: tuple[GDSPSLayerInfo, ...],
    files: tuple[GDSPSDatamartFile, ...],
    roi_bbox: tuple[float, float, float, float] | None = None,
) -> tuple[str, ...]:
    """Return the storm-surge models actually discovered, in canonical order.

    GDSPS and RESPS are surfaced as separate models so the UI never mixes a
    deterministic run with an ensemble member.  Datamart NetCDF files are
    GDSPS-only, so their presence implies GDSPS availability.

    When ``roi_bbox`` is given, a model is offered only if at least one of its
    layers geographically covers the ROI.  This is what stops the regional
    RESPS (Atlantic North-West) from being offered for a Pacific or Arctic ROI
    where its tiles would be blank; the global GDSPS always covers any ROI.
    """

    present = {
        layer.model
        for layer in layers
        if roi_bbox is None or bbox_intersects(layer.bbox, roi_bbox)
    }
    if files:
        # The GDSPS Datamart grid is global, so it always covers the ROI.
        present.add(GDSPS_MODEL)
    return tuple(model for model in SURGE_MODELS if model in present)


def runs_from_reference_times(
    layers: tuple[GDSPSLayerInfo, ...],
    model: str,
    variable: str,
) -> tuple[GDSPSRun, ...]:
    """Return model runs from the WMS ``reference_time`` dimension, newest first.

    GeoMet advertises each issuance as a ``reference_time``; this is a more
    reliable run source than crawling Datamart filenames and works for both
    GDSPS and RESPS.
    """

    reference_times: set[datetime] = set()
    for layer in layers:
        if layer.model == model and layer.variable == variable:
            reference_times.update(layer.reference_times)
    return tuple(
        GDSPSRun(issue_time=value, cycle=f"{value.hour:02d}")
        for value in sorted(reference_times, reverse=True)
    )


def variables_for_model(
    layers: tuple[GDSPSLayerInfo, ...],
    files: tuple[GDSPSDatamartFile, ...],
    model: str,
) -> tuple[str, ...]:
    """Return the variables discovered for one model, in canonical order."""

    discovered = {
        layer.variable
        for layer in layers
        if layer.model == model and layer.variable
    }
    if model == GDSPS_MODEL:
        discovered.update(file.variable for file in files)
    return tuple(
        variable for variable in GDSPS_VARIABLES if variable in discovered
    )


def members_for_model(
    layers: tuple[GDSPSLayerInfo, ...],
    model: str,
    variable: str,
) -> tuple[int, ...]:
    """Return the sorted ensemble members for a model/variable (RESPS only)."""

    members = {
        layer.member
        for layer in layers
        if layer.model == model
        and layer.variable == variable
        and layer.member is not None
    }
    return tuple(sorted(members))


def layer_for(
    layers: tuple[GDSPSLayerInfo, ...],
    model: str,
    variable: str,
    member: int | None = None,
) -> GDSPSLayerInfo | None:
    """Return the discovered WMS layer for a model/variable/member, if any."""

    for layer in layers:
        if (
            layer.model == model
            and layer.variable == variable
            and layer.member == member
        ):
            return layer
    return None


def layer_for_variable(
    layers: tuple[GDSPSLayerInfo, ...],
    variable: str,
) -> GDSPSLayerInfo | None:
    """Return the first discovered WMS layer for a variable, if any.

    Retained for callers that are not model-aware; new code should prefer
    :func:`layer_for`, which keeps GDSPS and RESPS members distinct.
    """

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
    model: str,
    variable: str,
    member: int | None,
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
    """Fetch a ROI-masked subset for one model/variable/member.

    GeoMet serves the storm-surge coverages as a single *latest* 2-D slice with
    no WCS time axis (verified live: ``axisLabels="lat long"``), so a
    time-specific request cannot be honoured over WCS. The MSC Datamart carries
    the full per-lead-time forecast series, but for GDSPS only — the RESPS
    Datamart tree has no per-member files. Therefore:

    * GDSPS is served from the Datamart (any forecast time), falling back to the
      WCS latest slice if the Datamart is unavailable.
    * RESPS is served from the WCS latest slice only; a specific ensemble member
      never falls through to GDSPS numbers.

    Network operations are injected so this orchestration is offline-testable.
    Returns the processed subset and a human-readable source-service label.
    """

    # GDSPS: prefer the Datamart, which is the only source with a real forecast
    # time series. The WCS coverage is a single latest slice.
    if model == GDSPS_MODEL:
        try:
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
        except (
            GDSPSDataUnavailableError,
            GDSPSResponseError,
            GDSPSRequestError,
        ):
            # Fall back to the WCS latest slice below.
            pass

    # WCS latest slice — the only numerical source for a RESPS member, and the
    # GDSPS fallback. The coverage has no time axis, so no time subset is sent.
    coverages, _ = wcs_coverages()
    coverage = find_coverage(coverages, model, variable, member)
    data = wcs_bytes(coverage.coverage_id, bbox, None)
    subset = subset_netcdf_bytes(data, roi=roi, variable=variable)
    return subset, "GeoMet WCS (latest slice)"
