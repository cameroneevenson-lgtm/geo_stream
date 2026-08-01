"""Streamlit entry point for the Geo Stream coastal flood explorer."""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import sys
from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import uuid4

# Streamlit Community Cloud runs app.py from /mount/src/<repo>. Ensure the
# repo root stays on sys.path before local package imports (also covers hosts
# that put only the parent of the repo on the path).
_APP_DIR = Path(__file__).resolve().parent
_app_dir_str = str(_APP_DIR)
if _app_dir_str not in sys.path:
    sys.path.insert(0, _app_dir_str)

import streamlit as st
from streamlit_folium import st_folium

from coastal_flood_explorer.animation import (
    AnimationError,
    build_forecast_animation,
    filter_by_publication_time,
    prepare_timeline_data,
    publication_times,
)
from coastal_flood_explorer.api import MAX_TOTAL_FEATURES
from coastal_flood_explorer.archive import (
    ARCHIVE_BASE_URL,
    MAX_ARCHIVE_FILES,
    ArchiveError,
    ArchiveFetchResult,
    ECCCDatamartArchiveClient,
    ECCCArchiveRequestError,
)
from coastal_flood_explorer.archive_dates import (
    ArchiveDateWindow,
    recent_archive_window,
)
from coastal_flood_explorer.archive_range import (
    ArchiveRangeFetchResult,
    combine_archive_range,
    inclusive_archive_dates,
    raw_range_bundle_bytes,
)
from coastal_flood_explorer.chs import (
    CHART_OBSERVED_COLUMN,
    CHART_PREDICTED_COLUMN,
    CHART_TIME_COLUMN,
    CHS_API_ROOT,
    DEFAULT_STATION_CODE,
    CHSClient,
    CHSError,
    CHSStation,
    CHSStationCatalog,
    CHSStationMatch,
    CHSWaterLevelBundle,
    StationProximity,
    floor_to_anchor,
    latest_point,
    nearest_point,
    raw_bundle_bytes as chs_raw_bundle_bytes,
    select_station_for_roi,
    water_level_chart_frame,
)
from coastal_flood_explorer.filtering import (
    ALL_FORECAST_PERIODS,
    FilterCriteria,
    filter_features,
    forecast_period_options,
    summarize_features,
)
from coastal_flood_explorer.feedback import (
    REPORT_LABELS,
    FeedbackError,
    build_issue_body,
    create_github_issue,
    format_issue_title,
    get_app_version,
    get_current_page_context,
    get_deployment_environment,
    github_config_from_secrets,
    sanitize_session_state,
)
from coastal_flood_explorer.geometry import (
    GeometryError,
    clip_feature_collection,
    rank_points_for_roi,
    roi_bbox,
)
from coastal_flood_explorer.gdsps_common import (
    GDSPS_DATAMART_ROOT,
    GDSPS_DATAMART_SUBPATH,
    GDSPS_MODEL,
    MODEL_DEFINITIONS,
    RESPS_MODEL,
    GDSPSError,
    GDSPSRun,
    gdsps_datamart_base_path,
)
from coastal_flood_explorer import gdsps_service
from coastal_flood_explorer.gdsps_datamart import GDSPSDatamartClient
from coastal_flood_explorer.gdsps_export import build_export_zip
from coastal_flood_explorer.gdsps_wcs import GDSPSWCSClient
from coastal_flood_explorer.gdsps_wms import (
    GEOMET_WMS_URL,
    GDSPSWMSClient,
    build_wms_tile_params,
)
from coastal_flood_explorer.map_view import (
    build_base_map,
    build_chs_station_layer,
    build_drawing_hydration_layer,
    build_gdsps_overlay_layer,
    build_layer_control,
    build_result_layer,
    risk_legend_html,
)

# CaSR-Land is optional at import time so a Cloud-only import failure cannot
# blank the whole map. The real exception text is kept for the sidebar
# (Streamlit Cloud redacts it from the main crash screen).
try:
    from coastal_flood_explorer.casr_common import CASR_HPFX_ROOT, CASRError
    from coastal_flood_explorer.casr_land import (
        CASR_LAND_DEFAULT,
        CASR_LAND_END,
        CASR_LAND_START,
        CASR_LAND_VARIABLES,
        LAND_AIR_TEMP,
        LAND_DEWPOINT,
        LAND_RUNOFF,
        LAND_SWE,
        LAND_VARIABLE_DEFINITIONS,
        LAND_WIND_U,
        LAND_WIND_V,
        download_land_day,
        fetch_land_for_roi,
    )
    from coastal_flood_explorer.map_view import build_casr_overlay_layer

    _CASR_IMPORT_ERROR: str | None = None
except ImportError as exc:
    CASR_HPFX_ROOT = "https://hpfx.collab.science.gc.ca"
    CASR_LAND_DEFAULT = date(2017, 12, 31)
    CASR_LAND_END = date(2017, 12, 31)
    CASR_LAND_START = date(1980, 1, 1)
    CASR_LAND_VARIABLES = (
        "TJ_1.5m",
        "TDK_1.5m",
        "TRAF_Aggregated",
        "SWE_Land",
        "UDC_10m",
        "VDC_10m",
    )
    LAND_AIR_TEMP = "TJ_1.5m"
    LAND_DEWPOINT = "TDK_1.5m"
    LAND_RUNOFF = "TRAF_Aggregated"
    LAND_SWE = "SWE_Land"
    LAND_WIND_U = "UDC_10m"
    LAND_WIND_V = "VDC_10m"
    LAND_VARIABLE_DEFINITIONS = {
        name: "CaSR-Land variable." for name in CASR_LAND_VARIABLES
    }

    class CASRError(RuntimeError):
        """Fallback when the CaSR package failed to import."""

    def download_land_day(*_args: Any, **_kwargs: Any) -> bytes:
        raise CASRError("CaSR-Land is unavailable in this deployment.")

    def fetch_land_for_roi(*_args: Any, **_kwargs: Any) -> Any:
        raise CASRError("CaSR-Land is unavailable in this deployment.")

    build_casr_overlay_layer = None  # type: ignore[assignment]
    _CASR_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"
    logging.getLogger("geo_stream.app").exception(
        "CaSR-Land modules failed to import; map will load without CaSR"
    )
from coastal_flood_explorer.properties import (
    CONTRIBUTOR_VALUES,
    RISK_LEVELS,
    export_filename,
    feature_collection_bytes,
    feature_collection_to_dataframe,
    format_utc_datetime,
)
from coastal_flood_explorer.state import (
    MAP_RETURNED_OBJECTS,
    reconcile_drawings,
    roi_matches,
)
from coastal_flood_explorer.synthetic import generate_synthetic_data


LOGGER = logging.getLogger("geo_stream.app")
REPOSITORY_URL = "https://github.com/cameroneevenson-lgtm/geo_stream"
MAP_COMPONENT_KEY = "coastal-flood-map-v9"
EMPTY_COLLECTION = {"type": "FeatureCollection", "features": []}
STATE_DEFAULTS: dict[str, Any] = {
    "drawings": [],
    "active_roi": None,
    "drawing_warnings": [],
    "last_successful_archive_response": None,
    "clipped_data": None,
    "last_requested_bbox": None,
    "last_requested_roi": None,
    "last_requested_archive_range": None,
    "fetch_timestamp": None,
    "current_source_mode": None,
    "clip_warnings": [],
    "raw_feature_count": 0,
    "clipped_feature_count": 0,
    "archive_product_count": 0,
    "archive_requested_date_count": 0,
    "archive_successful_date_count": 0,
    "archive_date_failures": [],
    "raw_archive_download": None,
    "chs_catalog": None,
    "chs_bundles": {},
    "chs_selection_roi": None,
    "selected_chs_station_id": None,
    "casr_enabled": True,
    "casr_overlay_params": None,
    "casr_opacity": 0.75,
    "casr_fetch_roi": None,
    "casr_point_series": None,
    "casr_subset_summary": None,
    "casr_warnings": [],
    "gdsps_enabled": False,
    "gdsps_overlay_params": None,
    "gdsps_opacity": 0.7,
    "gdsps_export_bytes": None,
    "gdsps_export_name": None,
    "gdsps_source_service": None,
    "gdsps_fetch_roi": None,
    "gdsps_point_series": None,
    "gdsps_subset_summary": None,
    "feedback_submission_in_progress": False,
    "feedback_last_submission_fingerprint": None,
    "feedback_last_success": None,
}


@st.cache_data(ttl=300, max_entries=128, show_spinner=False)
def _cached_archive_fetch(
    archive_root: str,
    archive_date: str,
) -> tuple[ArchiveFetchResult | None, str | None, bool]:
    """Fetch one issue and cache either its result or its safe failure."""

    try:
        result = ECCCDatamartArchiveClient(
            archive_root=archive_root,
        ).fetch_date(archive_date)
    except ArchiveError as exc:
        systemic = (
            isinstance(exc, ECCCArchiveRequestError)
            and exc.systemic
        )
        return None, str(exc), systemic
    return result, None, False


@st.cache_data(ttl=300, max_entries=4, show_spinner=False)
def _cached_chs_catalog(
    api_root: str,
) -> tuple[CHSStationCatalog | None, str | None, tuple[str, ...]]:
    """Fetch and cache the CHS catalogue, including safe failure outcomes."""

    try:
        return CHSClient(api_root=api_root).fetch_catalog(), None, ()
    except CHSError as exc:
        warnings = tuple(getattr(exc, "warnings", ()))
        return None, str(exc), warnings


@st.cache_data(ttl=300, max_entries=256, show_spinner=False)
def _cached_chs_bundle(
    api_root: str,
    station: CHSStation,
    anchor: datetime,
) -> tuple[CHSWaterLevelBundle | None, str | None, tuple[str, ...]]:
    """Fetch one station bundle while rate-limiting repeated failures."""

    try:
        return (
            CHSClient(api_root=api_root).fetch_bundle(station, anchor),
            None,
            (),
        )
    except CHSError as exc:
        warnings = tuple(getattr(exc, "warnings", ()))
        return None, str(exc), warnings


@st.cache_data(ttl=900, max_entries=4, show_spinner=False)
def _cached_gdsps_wms_layers(endpoint: str) -> tuple[tuple[Any, ...], str | None]:
    """Discover and cache GDSPS WMS layers, including safe failures."""

    try:
        return tuple(GDSPSWMSClient(endpoint).discover_layers()), None
    except GDSPSError as exc:
        return (), str(exc)


@st.cache_data(ttl=900, max_entries=4, show_spinner=False)
def _cached_gdsps_wcs_coverages(
    endpoint: str,
) -> tuple[tuple[Any, ...], str | None]:
    """Discover and cache GDSPS WCS coverages, including safe failures."""

    try:
        return tuple(GDSPSWCSClient(endpoint).discover_coverages()), None
    except GDSPSError as exc:
        return (), str(exc)


@st.cache_data(ttl=900, max_entries=4, show_spinner=False)
def _cached_gdsps_datamart_files(
    root: str,
    base_path: str,
) -> tuple[tuple[Any, ...], str | None]:
    """Discover and cache GDSPS Datamart NetCDF files, including failures."""

    try:
        client = GDSPSDatamartClient(root=root, base_path=base_path)
        return tuple(client.discover_files()), None
    except GDSPSError as exc:
        return (), str(exc)


@st.cache_data(ttl=900, max_entries=32, show_spinner=False)
def _cached_gdsps_wcs_bytes(
    endpoint: str,
    coverage_id: str,
    bbox: tuple[float, float, float, float],
    time: datetime | None,
) -> bytes:
    """Fetch and cache a WCS NetCDF subset keyed by primitive request args."""

    return GDSPSWCSClient(endpoint).fetch_coverage(
        coverage_id,
        bbox=bbox,
        time=time,
    )


@st.cache_data(ttl=900, max_entries=32, show_spinner=False)
def _cached_gdsps_datamart_bytes(
    root: str,
    base_path: str,
    url: str,
) -> bytes:
    """Fetch and cache one Datamart NetCDF file's bytes keyed by its URL."""

    client = GDSPSDatamartClient(root=root, base_path=base_path)
    return client.download(url)


@st.cache_data(ttl=3600, max_entries=8, show_spinner=False)
def _cached_casr_land_day(yyyymmdd: str) -> bytes:
    """Download one CaSR-Land day NetCDF keyed by YYYYMMDD (no ROI)."""

    day = datetime.strptime(yyyymmdd, "%Y%m%d").date()
    return download_land_day(day, root=CASR_HPFX_ROOT)


def _gdsps_datamart_base_paths() -> tuple[str, ...]:
    """Return today's and yesterday's date-prefixed GDSPS Datamart base paths.

    The MSC Datamart is organized under /YYYYMMDD/WXO-DD/…; the current UTC
    day's run may not be published yet early in the day, so yesterday is a
    fallback. Newest first. The date is part of each cache key, so entries
    rotate naturally as the day advances.
    """

    today = datetime.now(timezone.utc).date()
    return tuple(
        gdsps_datamart_base_path(today - timedelta(days=offset))
        for offset in (0, 1)
    )


def _discover_gdsps_datamart_files() -> tuple[tuple[Any, ...], str | None]:
    """Merge GDSPS Datamart discovery across the candidate dates.

    A per-date failure (e.g. today's run not published yet) is tolerated as
    long as another date yields files; an error is only surfaced when nothing
    was discovered at all.
    """

    merged: dict[str, Any] = {}
    last_error: str | None = None
    for base_path in _gdsps_datamart_base_paths():
        files, error = _cached_gdsps_datamart_files(GDSPS_DATAMART_ROOT, base_path)
        for discovered in files:
            merged[discovered.url] = discovered
        if error:
            last_error = error
    return tuple(merged.values()), (None if merged else last_error)


def _gdsps_datamart_base_path_for_url(url: str) -> str:
    """Return the date-prefixed base path that contains a Datamart file URL.

    The download primitive validates that a URL sits within its base path, so a
    file discovered under one date must be fetched with that same date's base
    path rather than today's.
    """

    marker = f"/{GDSPS_DATAMART_SUBPATH}"
    index = url.find(marker)
    if index != -1:
        path_start = url.find("/", url.find("://") + 3)
        if path_start != -1:
            return url[path_start : index + len(marker)]
    # Fall back to today's path; the download guard will reject a mismatch.
    return _gdsps_datamart_base_paths()[0]


def _initialize_state() -> None:
    for key, value in STATE_DEFAULTS.items():
        if key not in st.session_state:
            st.session_state[key] = copy.deepcopy(value)


def _utc_now() -> datetime:
    """Return the current aware UTC time through a testable seam."""

    return datetime.now(timezone.utc)


def _apply_map_drawings_payload(payload: object) -> bool:
    """Apply an authoritative component payload and report a state change."""

    if not isinstance(payload, Mapping):
        return False
    drawings_value = payload.get("all_drawings")
    if drawings_value is None:
        return False
    reconciled = reconcile_drawings(drawings_value)
    drawings = list(reconciled.drawings)
    changed = (
        st.session_state.get("drawings") != drawings
        or st.session_state.get("active_roi") != reconciled.active_roi
        or st.session_state.get("drawing_warnings")
        != list(reconciled.warnings)
    )
    st.session_state["drawings"] = drawings
    st.session_state["active_roi"] = reconciled.active_roi
    st.session_state["drawing_warnings"] = list(reconciled.warnings)
    return changed


def _sync_map_drawings() -> None:
    """Synchronize the component's authoritative drawing collection."""

    _apply_map_drawings_payload(st.session_state.get(MAP_COMPONENT_KEY))


def _store_dataset(
    *,
    raw_response: dict[str, Any],
    clipped_data: dict[str, Any],
    bbox: tuple[float, float, float, float],
    roi: Mapping[str, Any],
    source_mode: str,
    warnings: tuple[str, ...],
    archive_range: tuple[date, date] | None = None,
    archive_product_count: int = 0,
    archive_requested_date_count: int = 0,
    archive_successful_date_count: int = 0,
    archive_date_failures: tuple[Mapping[str, str], ...] = (),
    raw_archive_download: bytes | None = None,
) -> None:
    if source_mode == "archive":
        st.session_state["last_successful_archive_response"] = raw_response
        st.session_state["last_requested_archive_range"] = archive_range
        st.session_state["archive_product_count"] = archive_product_count
        st.session_state["archive_requested_date_count"] = (
            archive_requested_date_count
        )
        st.session_state["archive_successful_date_count"] = (
            archive_successful_date_count
        )
        st.session_state["archive_date_failures"] = [
            dict(failure) for failure in archive_date_failures
        ]
        st.session_state["raw_archive_download"] = raw_archive_download
    st.session_state["clipped_data"] = clipped_data
    st.session_state["last_requested_bbox"] = bbox
    st.session_state["last_requested_roi"] = copy.deepcopy(dict(roi))
    st.session_state["fetch_timestamp"] = _utc_now()
    st.session_state["current_source_mode"] = source_mode
    st.session_state["clip_warnings"] = list(warnings)
    raw_features = raw_response.get("features")
    clipped_features = clipped_data.get("features")
    st.session_state["raw_feature_count"] = (
        len(raw_features) if isinstance(raw_features, list) else 0
    )
    st.session_state["clipped_feature_count"] = (
        len(clipped_features) if isinstance(clipped_features, list) else 0
    )


def _current_bbox() -> tuple[float, float, float, float] | None:
    active_roi = st.session_state.get("active_roi")
    if active_roi is None:
        return None
    try:
        return roi_bbox(active_roi)
    except GeometryError:
        return None


def _results_are_stale() -> bool:
    if st.session_state.get("clipped_data") is None:
        return False
    return not roi_matches(
        st.session_state.get("active_roi"),
        st.session_state.get("last_requested_roi"),
    )


def _format_range(start: datetime | None, end: datetime | None) -> str:
    if start is None or end is None:
        return "—"
    start_text = format_utc_datetime(start)
    end_text = format_utc_datetime(end)
    return start_text if start_text == end_text else f"{start_text} → {end_text}"


def _normalize_archive_range(
    value: object,
    window: ArchiveDateWindow,
) -> tuple[date, date] | None:
    """Return a complete, ordered issue-date range inside the UTC window."""

    if (
        not isinstance(value, (tuple, list))
        or len(value) != 2
    ):
        return None
    start, end = value
    if (
        isinstance(start, datetime)
        or isinstance(end, datetime)
        or not isinstance(start, date)
        or not isinstance(end, date)
        or start > end
        or not window.contains(start)
        or not window.contains(end)
    ):
        return None
    try:
        inclusive_archive_dates(start, end)
    except ArchiveError:
        return None
    return start, end


def _archive_range_text(value: object) -> str:
    """Return a human-readable inclusive date range."""

    if (
        not isinstance(value, (tuple, list))
        or len(value) != 2
        or not all(
            isinstance(item, date) and not isinstance(item, datetime)
            for item in value
        )
    ):
        return "unknown range"
    start, end = value
    if start == end:
        return start.isoformat()
    return f"{start.isoformat()} through {end.isoformat()}"


def _archive_range_stamp(value: object) -> str:
    """Return a stable filename/key stamp for a loaded date range."""

    if (
        not isinstance(value, (tuple, list))
        or len(value) != 2
        or not all(
            isinstance(item, date) and not isinstance(item, datetime)
            for item in value
        )
    ):
        return "unknown"
    start, end = value
    return f"{start:%Y%m%d}_{end:%Y%m%d}"


def _archive_clipped_filename(
    base_name: str,
    archive_range: object,
    requested_count: int,
    successful_count: int,
) -> str:
    """Add range and partial-success provenance to a clipped filename."""

    completeness_stamp = (
        f"_partial_{successful_count}of{requested_count}"
        if requested_count and successful_count < requested_count
        else ""
    )
    return base_name.replace(
        "eccc_coastal_flooding_",
        (
            f"eccc_coastal_flooding_{_archive_range_stamp(archive_range)}"
            f"{completeness_stamp}_"
        ),
    )


def _archive_failure_details(
    result: ArchiveRangeFetchResult,
) -> tuple[dict[str, str], ...]:
    """Return safe diagnostics for issue dates that were not loaded."""

    return tuple(
        {
            "issue_date": datetime.strptime(
                outcome.issue_date,
                "%Y%m%d",
            ).date().isoformat(),
            "error_type": outcome.error_type or "ArchiveDateFailure",
            "message": outcome.error_message or "The date was not loaded.",
        }
        for outcome in result.outcomes
        if not outcome.succeeded
    )


def _default_chs_station(
    stations: tuple[CHSStation, ...],
) -> CHSStation:
    """Return Bedford Institute when available, otherwise the first station."""

    for station in stations:
        if station.code == DEFAULT_STATION_CODE:
            return station
    return stations[0]


def _chs_station_matches(
    stations: tuple[CHSStation, ...],
) -> tuple[CHSStationMatch, dict[str, CHSStationMatch]]:
    """Return the automatic station and exact-ROI metrics for every station."""

    active_roi = st.session_state.get("active_roi")
    default_station = _default_chs_station(stations)
    if active_roi is None:
        default_match = CHSStationMatch(
            station=default_station,
            inside_roi=False,
            distance_to_roi_km=0.0,
            distance_to_center_km=0.0,
        )
        return default_match, {default_station.id: default_match}

    point_matches = rank_points_for_roi(
        active_roi,
        (
            (station.id, station.longitude, station.latitude)
            for station in stations
        ),
    )
    proximities = tuple(
        StationProximity(
            station_id=match.point_id,
            inside_roi=match.inside_roi,
            distance_to_roi_km=match.distance_to_roi_km,
            distance_to_center_km=match.distance_to_center_km,
        )
        for match in point_matches
    )
    auto_match = select_station_for_roi(stations, proximities)
    stations_by_id = {station.id: station for station in stations}
    matches_by_id = {
        proximity.station_id: CHSStationMatch(
            station=stations_by_id[proximity.station_id],
            inside_roi=proximity.inside_roi,
            distance_to_roi_km=proximity.distance_to_roi_km,
            distance_to_center_km=proximity.distance_to_center_km,
        )
        for proximity in proximities
        if proximity.station_id in stations_by_id
    }
    return auto_match, matches_by_id


def _chs_roi_changed(current_roi: object, previous_roi: object) -> bool:
    """Return whether a drawing change should reset automatic station choice."""

    if current_roi is None:
        return previous_roi is not None
    if previous_roi is None:
        return True
    return not roi_matches(current_roi, previous_roi)


def _format_station_option(station: CHSStation) -> str:
    availability = (
        "observed + predicted"
        if station.offers("wlp")
        else "observed"
    )
    return f"{station.label} · {availability}"


def _observation_age_text(
    point_time: datetime,
    reference_time: datetime,
) -> str:
    seconds = max(0.0, (reference_time - point_time).total_seconds())
    minutes = int(round(seconds / 60.0))
    if minutes < 60:
        return f"{minutes} min"
    hours = minutes // 60
    remainder = minutes % 60
    return f"{hours} h {remainder} min" if remainder else f"{hours} h"


def _render_chs_bundle(
    bundle: CHSWaterLevelBundle,
    *,
    stale: bool,
    requested_station: CHSStation | None = None,
    fallback_reason: str | None = None,
) -> None:
    """Render truthful station metrics, chart, diagnostics, and raw download."""

    observation = latest_point(bundle.observed)
    prediction = nearest_point(bundle.predicted, bundle.anchor_time)
    rendered_at = _utc_now()
    observed_count = (
        len(bundle.observed.points) if bundle.observed is not None else 0
    )
    predicted_count = (
        len(bundle.predicted.points) if bundle.predicted is not None else 0
    )

    if (
        requested_station is not None
        and requested_station.id != bundle.station.id
    ):
        st.warning(
            f"Water-level data for {requested_station.label} could not be "
            f"loaded. Showing the last successful data from "
            f"{bundle.station.label}, retrieved "
            f"{format_utc_datetime(bundle.fetched_at)}. This fallback gauge "
            "may not represent the drawn region."
        )
        if fallback_reason:
            st.caption(f"Selected-station error: {fallback_reason}")
    elif stale:
        st.warning(
            "The CHS refresh failed, so this is the last successful data for "
            f"{bundle.station.label}, retrieved "
            f"{format_utc_datetime(bundle.fetched_at)}."
        )
    elif observed_count:
        st.success(
            "Official CHS observations loaded"
            f" · {observed_count} observed point(s)"
            + (
                f" · {predicted_count} tide-prediction point(s)"
                if predicted_count
                else ""
            )
        )
    else:
        st.info(
            "CHS observations were unavailable for this window. Showing "
            f"{predicted_count} explicitly labelled tide-prediction point(s)."
        )

    metric_columns = st.columns(4)
    metric_columns[0].metric(
        "Latest observation",
        f"{observation.value_m:.3f} m" if observation is not None else "—",
    )
    metric_columns[1].metric(
        "Observation age",
        (
            _observation_age_text(
                observation.timestamp,
                rendered_at,
            )
            if observation is not None
            else "Unavailable"
        ),
    )
    metric_columns[2].metric(
        "Observation QC",
        observation.qc_label if observation is not None else "—",
    )
    metric_columns[3].metric(
        "Tide prediction near now",
        f"{prediction.value_m:.3f} m" if prediction is not None else "—",
    )

    if observation is not None:
        review_text = (
            "reviewed"
            if observation.reviewed is True
            else "preliminary / not marked reviewed"
        )
        st.caption(
            "Latest observation: "
            f"{format_utc_datetime(observation.timestamp)} · {review_text}."
        )

    frame = water_level_chart_frame(bundle)
    chart_columns = [
        column
        for column in (CHART_OBSERVED_COLUMN, CHART_PREDICTED_COLUMN)
        if column in frame and frame[column].notna().any()
    ]
    if chart_columns:
        chart_colours = [
            "#0284c7" if column == CHART_OBSERVED_COLUMN else "#f97316"
            for column in chart_columns
        ]
        st.line_chart(
            frame,
            x=CHART_TIME_COLUMN,
            y=chart_columns,
            x_label="Time (UTC)",
            y_label="Water level (m, local Chart Datum)",
            color=chart_colours,
            height=330,
        )
    else:
        st.info("No chartable CHS points were returned for this station.")

    st.caption(
        "Heights are metres relative to this station's local Chart Datum "
        "(CD). CHS gauges are point measurements, not inundation maps. "
        "Do not directly compare absolute heights between stations without "
        "a documented datum conversion."
    )
    st.caption(
        "Source: [Official CHS station page]"
        "(https://www.tides.gc.ca/en/stations/"
        f"{quote(bundle.station.code, safe='')})"
        f" · Retrieved {format_utc_datetime(bundle.fetched_at)}"
    )

    if bundle.warnings:
        with st.expander(
            f"CHS retrieval details ({len(bundle.warnings)})"
        ):
            for warning in bundle.warnings:
                st.write(f"- {warning}")

    stamp = bundle.anchor_time.strftime("%Y%m%dT%H%MZ")
    st.download_button(
        "Download raw fetched CHS JSON",
        data=chs_raw_bundle_bytes(bundle),
        file_name=(
            f"chs_water_levels_{bundle.station.code}_{stamp}.json"
        ),
        mime="application/json",
        width="stretch",
    )


def _render_chs_water_levels(
) -> tuple[tuple[CHSStation, ...], str | None, CHSWaterLevelBundle | None]:
    """Render the always-on ROI-driven CHS water-level experience."""

    with st.container(border=True):
        heading_columns = st.columns([3, 1.5])
        with heading_columns[0]:
            st.subheader("CHS station water levels")
            st.caption(
                "Official CHS observations load automatically. A drawing "
                "selects a gauge inside the exact ROI, or the nearest gauge "
                "when none lies inside."
            )
        refresh = heading_columns[1].button(
            "Refresh CHS",
            width="stretch",
            help=(
                "Clear the CHS caches and request 24 hours of observations "
                "plus available tide predictions at 15-minute resolution."
            ),
        )
        if refresh:
            _cached_chs_catalog.clear()
            _cached_chs_bundle.clear()

        load_status = st.status(
            "Loading official CHS station and water-level data…",
            expanded=False,
            state="running",
        )
        try:
            catalog, catalog_error, catalog_warnings = _cached_chs_catalog(
                CHS_API_ROOT
            )
        except Exception:
            LOGGER.exception("Unexpected failure loading the CHS catalogue")
            catalog = None
            catalog_error = (
                "An unexpected error occurred while loading CHS stations."
            )
            catalog_warnings = ()

        previous_catalog = st.session_state.get("chs_catalog")
        catalog_stale = False
        if catalog is not None:
            st.session_state["chs_catalog"] = catalog
        elif isinstance(previous_catalog, CHSStationCatalog):
            catalog = previous_catalog
            catalog_stale = True

        if catalog is None or not catalog.stations:
            load_status.update(
                label="CHS water levels are currently unavailable",
                state="error",
                expanded=True,
            )
            st.error(
                catalog_error
                or "CHS did not return an operating station catalogue."
            )
            for warning in catalog_warnings:
                st.warning(warning)
            return (), None, None

        stations = catalog.stations
        match_error: str | None = None
        try:
            auto_match, matches_by_id = _chs_station_matches(stations)
        except (CHSError, GeometryError) as exc:
            LOGGER.warning(
                "Could not match CHS stations to the ROI: %s",
                exc,
                exc_info=True,
            )
            default_station = _default_chs_station(stations)
            auto_match = CHSStationMatch(
                station=default_station,
                inside_roi=False,
                distance_to_roi_km=0.0,
                distance_to_center_km=0.0,
            )
            matches_by_id = {}
            match_error = str(exc)

        current_roi = st.session_state.get("active_roi")
        previous_selection_roi = st.session_state.get("chs_selection_roi")
        station_ids = {station.id for station in stations}
        selected_id = st.session_state.get("selected_chs_station_id")
        if (
            _chs_roi_changed(current_roi, previous_selection_roi)
            or selected_id not in station_ids
        ):
            st.session_state["selected_chs_station_id"] = (
                auto_match.station.id
            )
        st.session_state["chs_selection_roi"] = copy.deepcopy(current_roi)

        stations_by_id = {station.id: station for station in stations}
        selected_id = st.selectbox(
            "CHS water-level station",
            [station.id for station in stations],
            key="selected_chs_station_id",
            format_func=lambda station_id: _format_station_option(
                stations_by_id[station_id]
            ),
            help=(
                "The drawing chooses this automatically. You can override it "
                "with any operating CHS observation station."
            ),
        )
        selected_station = stations_by_id[selected_id]

        selected_match = matches_by_id.get(selected_id)
        if current_roi is None:
            if selected_id == auto_match.station.id:
                st.info(
                    f"No region is drawn, so {selected_station.label} is the "
                    "national default."
                )
            else:
                st.info(
                    f"No region is drawn. {selected_station.label} is your "
                    "manual station selection."
                )
        elif match_error is not None:
            st.warning(
                "The station-to-region distance could not be calculated. "
                f"{selected_station.label} is shown as a fallback station; "
                f"its distance from the drawing is unavailable. {match_error}"
            )
        elif selected_match is not None and selected_match.inside_roi:
            st.success(
                f"{selected_station.label} lies inside the exact drawn region."
            )
        elif selected_match is not None:
            prefix = (
                "No operating CHS observation station lies inside this "
                "region. The nearest gauge is"
                if selected_id == auto_match.station.id
                else "The manually selected gauge is"
            )
            st.info(
                f"{prefix} {selected_station.label}, "
                f"{selected_match.distance_to_roi_km:.1f} km outside the "
                "exact boundary."
            )

        anchor = floor_to_anchor(_utc_now())
        try:
            bundle, bundle_error, bundle_warnings = _cached_chs_bundle(
                CHS_API_ROOT,
                selected_station,
                anchor,
            )
        except Exception:
            LOGGER.exception(
                "Unexpected CHS water-level failure for %s",
                selected_station.id,
            )
            bundle = None
            bundle_error = (
                "An unexpected error occurred while loading CHS water levels."
            )
            bundle_warnings = ()

        saved_bundles = st.session_state.get("chs_bundles")
        if not isinstance(saved_bundles, dict):
            saved_bundles = {}
        bundle_stale = False
        fallback_from: CHSStation | None = None
        if bundle is not None:
            saved_bundles = dict(saved_bundles)
            saved_bundles[selected_station.id] = bundle
            st.session_state["chs_bundles"] = saved_bundles
        else:
            previous_bundle = saved_bundles.get(selected_station.id)
            if isinstance(previous_bundle, CHSWaterLevelBundle):
                bundle = previous_bundle
                bundle_stale = True
            else:
                fallback_candidates = [
                    candidate
                    for candidate in saved_bundles.values()
                    if (
                        isinstance(candidate, CHSWaterLevelBundle)
                        and candidate.station.id != selected_station.id
                    )
                ]
                if fallback_candidates:
                    bundle = max(
                        fallback_candidates,
                        key=lambda candidate: (
                            candidate.fetched_at,
                            candidate.station.id,
                        ),
                    )
                    bundle_stale = True
                    fallback_from = selected_station

        if bundle is None:
            load_status.update(
                label=(
                    f"CHS data could not be loaded for "
                    f"{selected_station.label}"
                ),
                state="error",
                expanded=True,
            )
            st.error(
                bundle_error
                or "CHS returned no usable water-level data for this station."
            )
            for warning in bundle_warnings:
                st.warning(warning)
            return stations, selected_station.id, None

        status_state = "error" if bundle_stale or catalog_stale else "complete"
        status_label = (
            (
                "Selected CHS station unavailable — showing fallback data "
                f"from {bundle.station.label}"
            )
            if fallback_from is not None
            else (
                "CHS refresh failed — showing the last successful station data"
                if bundle_stale
                else (
                    f"CHS water levels loaded for {selected_station.label}"
                    + (
                        " using the last successful station catalogue"
                        if catalog_stale
                        else ""
                    )
                )
            )
        )
        load_status.update(
            label=status_label,
            state=status_state,
            expanded=False,
        )
        if catalog_error and catalog_stale:
            st.warning(
                f"{catalog_error} The last successful station catalogue is "
                "still in use."
            )
        _render_chs_bundle(
            bundle,
            stale=bundle_stale,
            requested_station=fallback_from,
            fallback_reason=bundle_error,
        )
        return stations, bundle.station.id, bundle


def _gdsps_fetch_numeric(
    model: str,
    variable: str,
    member: int | None,
    bbox: tuple[float, float, float, float],
    roi: Mapping[str, Any],
    valid_time: datetime | None,
    run: GDSPSRun | None,
) -> tuple[Any, str]:
    """Bind the app's cached fetchers to the pure GDSPS orchestration."""

    return gdsps_service.fetch_numeric(
        model,
        variable,
        member,
        bbox,
        roi,
        valid_time,
        run,
        wcs_coverages=lambda: _cached_gdsps_wcs_coverages(GEOMET_WMS_URL),
        wcs_bytes=lambda coverage_id, box, time: _cached_gdsps_wcs_bytes(
            GEOMET_WMS_URL, coverage_id, box, time
        ),
        datamart_files=_discover_gdsps_datamart_files,
        datamart_bytes=lambda url: _cached_gdsps_datamart_bytes(
            GDSPS_DATAMART_ROOT, _gdsps_datamart_base_path_for_url(url), url
        ),
    )


def _render_casr_controls(
    bbox: tuple[float, float, float, float] | None,
    active_roi: Mapping[str, Any] | None,
) -> None:
    """Render CaSR-Land controls (currently the only exposed data layer)."""

    st.header("CaSR-Land")
    st.caption(
        "ECCC Canadian Surface Reanalysis (CaSR-Land v2.1) on the North "
        "American land/surface grid. Historical reanalysis through "
        f"{CASR_LAND_END:%Y-%m-%d} — not a flood warning, tide gauge, or "
        "live coastal forecast. Each day file is large (~110 MB); draw a "
        "compact coastal ROI before fetching."
    )
    if _CASR_IMPORT_ERROR:
        st.error(
            "CaSR-Land could not be loaded in this deployment. Import error: "
            f"{_CASR_IMPORT_ERROR}"
        )
        return
    enabled = st.checkbox(
        "Show CaSR-Land on the map",
        key="casr_enabled",
        help=(
            "Opacity and visibility changes never re-download the day file. "
            "Fetching NetCDF is a separate explicit action after you draw a "
            "region."
        ),
    )
    # Day is chosen offline from the published CaSR-Land window. Network
    # contact stays on the explicit Fetch button so the map can render first.
    day_value = st.date_input(
        "Reanalysis day (UTC)",
        value=CASR_LAND_DEFAULT,
        min_value=CASR_LAND_START,
        max_value=CASR_LAND_END,
        key="casr_selected_day",
        help=(
            "CaSR-Land v2.1 publishes one NetCDF per day "
            f"({CASR_LAND_START:%Y-%m-%d} to {CASR_LAND_END:%Y-%m-%d}). "
            f"Defaults to the latest published day ({CASR_LAND_DEFAULT:%Y-%m-%d})."
        ),
    )
    if isinstance(day_value, tuple):
        day_value = day_value[0] if day_value else CASR_LAND_DEFAULT
    if not isinstance(day_value, date):
        day_value = CASR_LAND_DEFAULT
    variable = st.selectbox(
        "Variable",
        CASR_LAND_VARIABLES,
        format_func=lambda code: {
            LAND_AIR_TEMP: "TJ_1.5m — air temperature at 1.5 m (°C)",
            LAND_DEWPOINT: "TDK_1.5m — dew point at 1.5 m (°C)",
            LAND_RUNOFF: "TRAF_Aggregated — accumulated surface runoff",
            LAND_SWE: "SWE_Land — snow water equivalent",
            LAND_WIND_U: "UDC_10m — 10 m U wind (m/s)",
            LAND_WIND_V: "VDC_10m — 10 m V wind (m/s)",
        }.get(code, code),
        key="casr_selected_variable",
    )
    st.caption(LAND_VARIABLE_DEFINITIONS[variable])
    opacity = st.slider(
        "CaSR overlay opacity",
        min_value=0.0,
        max_value=1.0,
        step=0.05,
        key="casr_opacity",
        help="Adjusting opacity never re-downloads HPFX NetCDF files.",
    )

    existing = st.session_state.get("casr_overlay_params")
    if enabled and isinstance(existing, Mapping) and existing.get("overlays"):
        updated = dict(existing)
        updated["opacity"] = float(opacity)
        st.session_state["casr_overlay_params"] = updated

    if bbox is None:
        st.info(
            "No region selected yet. Use the polygon or rectangle button "
            "in the map's upper-left drawing toolbar to draw within Canada."
        )
    else:
        st.success("Region selected — CaSR-Land fetch is ready.")
        st.caption("Active ROI bounds (CRS84: lon, lat)")
        st.code(
            "\n".join(
                (
                    f"minLon: {bbox[0]:.6f}",
                    f"minLat: {bbox[1]:.6f}",
                    f"maxLon: {bbox[2]:.6f}",
                    f"maxLat: {bbox[3]:.6f}",
                )
            ),
            language=None,
        )

    for warning in st.session_state.get("drawing_warnings", []):
        st.warning(warning)

    fetch = st.button(
        "Fetch CaSR-Land for ROI",
        type="primary",
        disabled=bbox is None or active_roi is None,
        width="stretch",
        help=(
            "Draw a region first."
            if bbox is None
            else "Download the selected day and mask it to the drawn ROI."
        ),
    )
    if fetch and active_roi is not None:
        _run_casr_fetch(day_value, variable, active_roi, float(opacity))

    stale = bool(
        st.session_state.get("casr_overlay_params")
        and not roi_matches(active_roi, st.session_state.get("casr_fetch_roi"))
    )
    if stale:
        st.warning(
            "The CaSR overlay was fetched for a previous drawing. Fetch again "
            "for the current ROI."
        )


def _run_casr_fetch(
    day: date,
    variable: str,
    active_roi: Mapping[str, Any],
    opacity: float,
) -> None:
    status = st.status("Fetching CaSR-Land for the drawn region…", expanded=False)
    try:
        with status:
            st.write(
                f"Downloading CaSR-Land day {day:%Y-%m-%d} from HPFX "
                "(~110 MB; may take a minute)…"
            )

            def download(selected_day: date) -> bytes:
                return _cached_casr_land_day(selected_day.strftime("%Y%m%d"))

            subset = fetch_land_for_roi(
                day=day,
                variable=variable,
                roi=active_roi,
                download=download,
            )
            import base64

            overlays = [
                {
                    "png_b64": base64.b64encode(subset.overlay_png).decode(
                        "ascii"
                    ),
                    "bounds": [
                        list(subset.overlay_bounds[0]),
                        list(subset.overlay_bounds[1]),
                    ],
                    "point": list(subset.point),
                    "subbasin_id": subset.subbasin_id,
                }
            ]
            label = f"CaSR-Land · {subset.variable} · {day:%Y-%m-%d}"
            st.session_state["casr_overlay_params"] = {
                "label": label,
                "opacity": opacity,
                "overlays": overlays,
            }
            st.session_state["casr_fetch_roi"] = copy.deepcopy(active_roi)
            st.session_state["casr_warnings"] = list(subset.warnings)
            st.session_state["casr_point_series"] = subset.point_series
            st.session_state["casr_subset_summary"] = (
                f"{subset.variable} · {day:%Y-%m-%d}"
                + (f" · {subset.units}" if subset.units else "")
            )
            status.update(label="CaSR-Land fetch complete.", state="complete")
    except CASRError as exc:
        status.update(label="CaSR-Land fetch failed.", state="error")
        st.error(str(exc))
    except Exception:
        LOGGER.exception("Unexpected CaSR-Land fetch failure")
        status.update(label="CaSR-Land fetch failed.", state="error")
        st.error("CaSR-Land data could not be fetched for this region.")


def _render_casr_results() -> None:
    """Show CaSR-Land point series under the map."""

    summary = st.session_state.get("casr_subset_summary")
    series = st.session_state.get("casr_point_series")
    if not summary and series is None:
        return
    st.subheader("CaSR-Land")
    if summary:
        st.caption(summary)
    for warning in st.session_state.get("casr_warnings") or []:
        if isinstance(warning, str) and warning.strip():
            st.warning(warning)
    if series is not None:
        st.caption(
            "Sample-point time series nearest the ROI centroid. Historical "
            "reanalysis — not a warning product."
        )
        try:
            chart_frame = series.rename(
                columns={
                    "time_utc": "time",
                    "value": "CaSR value",
                }
            )
            st.line_chart(chart_frame, x="time", y="CaSR value")
        except Exception:
            LOGGER.exception("CaSR chart rendering failed")
            st.dataframe(series, width="stretch")


def _render_gdsps_controls(
    bbox: tuple[float, float, float, float] | None,
    active_roi: Mapping[str, Any] | None,
) -> None:
    """Render the GDSPS storm-surge sidebar section and set overlay state."""

    st.divider()
    st.header("Coastal storm surge (GDSPS / RESPS)")
    st.caption(
        "ECCC coastal storm-surge models. ETAS is storm-surge elevation; SSH is "
        "total water level (not an engineering or chart datum). The two "
        "variables are never interchanged, and GDSPS (deterministic) is never "
        "mixed with RESPS (ensemble)."
    )
    enabled = st.checkbox(
        "Enable storm-surge overlay",
        key="gdsps_enabled",
        help=(
            "Overlays the selected GeoMet WMS storm-surge layer on the map. The "
            "numerical subset is a separate, explicit fetch."
        ),
    )
    if st.button("Refresh available runs", width="stretch"):
        _cached_gdsps_wms_layers.clear()
        _cached_gdsps_wcs_coverages.clear()
        _cached_gdsps_datamart_files.clear()

    try:
        layers, layers_error = _cached_gdsps_wms_layers(GEOMET_WMS_URL)
        files, files_error = _discover_gdsps_datamart_files()
    except Exception:
        LOGGER.exception("Unexpected GDSPS discovery failure")
        st.session_state["gdsps_overlay_params"] = None
        st.error("GDSPS discovery failed unexpectedly.")
        return

    all_models = gdsps_service.models_available(layers, files)
    models = gdsps_service.models_available(layers, files, roi_bbox=bbox)
    if not models:
        st.session_state["gdsps_overlay_params"] = None
        if all_models and bbox is not None:
            # Models exist but none cover the drawn ROI (e.g. regional RESPS
            # with a Pacific ROI). This is expected, not an error.
            hidden = ", ".join(all_models)
            st.info(
                f"No storm-surge model covers the drawn region. Available "
                f"model(s) — {hidden} — do not include the ROI in their "
                "forecast domain."
            )
            return
        message = (
            "Storm-surge content is not currently advertised by GeoMet and no "
            "Datamart NetCDF files were discovered. This is not an error — the "
            "product may be temporarily unavailable."
        )
        st.info(message)
        for error in (layers_error, files_error):
            if error:
                st.caption(error)
        return

    model = st.selectbox(
        "Model",
        models,
        format_func=lambda code: (
            "GDSPS — Global Deterministic"
            if code == GDSPS_MODEL
            else "RESPS — Regional Ensemble"
        ),
        key="gdsps_selected_model",
    )
    st.caption(MODEL_DEFINITIONS[model])

    variable_options = gdsps_service.variables_for_model(layers, files, model)
    if not variable_options:
        st.session_state["gdsps_overlay_params"] = None
        st.info(f"No storm-surge variables are currently advertised for {model}.")
        return

    variable = st.selectbox(
        "Variable",
        variable_options,
        format_func=lambda code: (
            f"{code} — storm-surge elevation"
            if code == "ETAS"
            else f"{code} — total water level"
        ),
        key="gdsps_selected_variable",
    )

    member: int | None = None
    if model == RESPS_MODEL:
        members = gdsps_service.members_for_model(layers, model, variable)
        if members:
            member = st.selectbox(
                "Ensemble member",
                members,
                format_func=lambda number: (
                    f"{number:02d} (control)" if number == 1 else f"{number:02d}"
                ),
                key="gdsps_selected_member",
                help=(
                    "RESPS is an ensemble. Members are shown individually and "
                    "are never averaged."
                ),
            )

    layer = gdsps_service.layer_for(layers, model, variable, member)
    # Prefer the WMS reference_time dimension (works for both models); fall back
    # to dated GDSPS Datamart runs only when GeoMet advertises no reference_time.
    runs = gdsps_service.runs_from_reference_times(layers, model, variable)
    if not runs and model == GDSPS_MODEL:
        runs = gdsps_service.runs_from_files(files, variable)
    run: GDSPSRun | None = None
    if runs:
        run = st.selectbox(
            "Model run",
            runs,
            format_func=lambda item: item.label,
            key="gdsps_selected_run",
        )
    else:
        st.caption(
            "No dated model runs were advertised; using the current GeoMet "
            "layer state."
        )

    valid_times = gdsps_service.valid_times(layer, files, variable, run)
    valid_time: datetime | None = None
    if valid_times:
        valid_time = st.selectbox(
            "Forecast-valid time (UTC)",
            valid_times,
            format_func=format_utc_datetime,
            key="gdsps_selected_valid_time",
        )
    else:
        st.caption("No forecast-valid times were advertised for this variable.")

    opacity = st.slider(
        "Overlay opacity",
        min_value=0.0,
        max_value=1.0,
        step=0.05,
        key="gdsps_opacity",
        help="Adjusting opacity never triggers another download.",
    )

    if enabled and layer is not None:
        st.session_state["gdsps_overlay_params"] = build_wms_tile_params(
            layer,
            time=valid_time,
            opacity=float(opacity),
        )
    else:
        st.session_state["gdsps_overlay_params"] = None
        if enabled and layer is None:
            st.warning(
                f"GeoMet does not currently advertise a WMS overlay for "
                f"{model} {variable}. The numerical subset may still be "
                "available."
            )

    # The numerical subset is retrieved via GeoMet WCS for both models (per
    # ensemble member for RESPS); the MSC Datamart NetCDF fallback applies to
    # the deterministic GDSPS only. A RESPS request can never fall through to
    # GDSPS numbers — the coverage is selected by model and member.
    member_text = "" if member is None else f" member {member:02d}"
    fetch = st.button(
        f"Fetch {model}{member_text} numerical subset",
        type="primary",
        disabled=bbox is None or active_roi is None,
        width="stretch",
        help=(
            "Draw a region first."
            if bbox is None
            else "Retrieve only the drawn region and selected time."
        ),
    )
    if fetch and active_roi is not None:
        _run_gdsps_fetch(model, variable, member, bbox, active_roi, valid_time, run)
    _render_gdsps_download(active_roi)


def _run_gdsps_fetch(
    model: str,
    variable: str,
    member: int | None,
    bbox: tuple[float, float, float, float] | None,
    active_roi: Mapping[str, Any],
    valid_time: datetime | None,
    run: GDSPSRun | None,
) -> None:
    if bbox is None:
        return
    member_text = "" if member is None else f" member {member:02d}"
    label = f"{model}{member_text} {variable}"
    status = st.status(
        f"Fetching {label} numerical subset…",
        expanded=False,
        state="running",
    )
    try:
        with status:
            subset, service = _gdsps_fetch_numeric(
                model,
                variable,
                member,
                bbox,
                active_roi,
                valid_time,
                run,
            )
            export_bytes = build_export_zip(
                subset,
                roi=active_roi,
                source_service=service,
                run=run,
                model=model,
                member=member,
            )
        stamp = (
            valid_time.strftime("%Y%m%dT%H%M%SZ")
            if valid_time is not None
            else "latest"
        )
        member_slug = "" if member is None else f"_m{member:02d}"
        st.session_state["gdsps_export_bytes"] = export_bytes
        st.session_state["gdsps_export_name"] = (
            f"{model.lower()}{member_slug}_{variable.lower()}_{stamp}.zip"
        )
        st.session_state["gdsps_source_service"] = service
        st.session_state["gdsps_fetch_roi"] = copy.deepcopy(dict(active_roi))
        st.session_state["gdsps_point_series"] = subset.point_series
        st.session_state["gdsps_subset_summary"] = {
            "variable": subset.variable,
            "variable_name": subset.variable_name,
            "units": subset.units,
            "service": service,
            "cells": int(
                subset.dataset[subset.variable_name].notnull().sum().item()
            ),
            "warnings": list(subset.warnings),
        }
        status.update(
            label=f"{label} subset loaded from {service}",
            state="complete",
            expanded=False,
        )
    except (GDSPSError, GeometryError) as exc:
        LOGGER.warning("GDSPS fetch failed: %s", exc, exc_info=True)
        status.update(
            label="GDSPS numerical fetch failed",
            state="error",
            expanded=True,
        )
        st.error(str(exc))
    except Exception:
        LOGGER.exception("Unexpected GDSPS numerical failure")
        status.update(
            label="GDSPS numerical fetch failed",
            state="error",
            expanded=True,
        )
        st.error(
            "An unexpected error occurred while fetching the GDSPS subset."
        )


def _render_gdsps_download(active_roi: Mapping[str, Any] | None) -> None:
    export_bytes = st.session_state.get("gdsps_export_bytes")
    if not isinstance(export_bytes, (bytes, bytearray)):
        return
    fetch_roi = st.session_state.get("gdsps_fetch_roi")
    stale = not roi_matches(active_roi, fetch_roi)
    if stale:
        st.info(
            "The GDSPS subset was fetched for a different region. Fetch again "
            "before downloading."
        )
    st.download_button(
        "Download GDSPS export package (ZIP)",
        data=bytes(export_bytes),
        file_name=st.session_state.get("gdsps_export_name")
        or "gdsps_export.zip",
        mime="application/zip",
        disabled=stale,
        width="stretch",
    )


def _render_gdsps_results() -> None:
    """Render the GDSPS numerical subset summary and point series (no chart)."""

    summary = st.session_state.get("gdsps_subset_summary")
    if not isinstance(summary, Mapping):
        return
    st.subheader("GDSPS storm-surge subset")
    variable = summary.get("variable")
    definition = (
        "Storm-surge elevation (not total water level)."
        if variable == "ETAS"
        else "Total water level / sea-surface height (not an engineering datum)."
    )
    st.caption(
        f"Variable **{variable}** — {definition} Source: "
        f"{summary.get('service', 'unknown')}."
    )
    columns = st.columns(3)
    columns[0].metric("Variable", str(variable))
    columns[1].metric("Units", str(summary.get("units") or "—"))
    columns[2].metric("Masked grid cells", summary.get("cells", 0))
    point_series = st.session_state.get("gdsps_point_series")
    if point_series is not None:
        st.caption(
            "Point time series at the ROI representative point (tabular, not a "
            "separate chart):"
        )
        st.dataframe(point_series, width="stretch", hide_index=True)
    for warning in summary.get("warnings", []):
        st.caption(f"Note: {warning}")


def _render_feedback_form() -> None:
    """Render the repository-backed feedback form in the shared sidebar."""

    with st.expander("Feedback / Report a Bug", expanded=False):
        st.caption(
            "Send a bug report, suggestion, or other feedback directly to "
            "the Geo Stream GitHub repository."
        )
        in_progress = bool(
            st.session_state.get("feedback_submission_in_progress")
        )
        with st.form("feedback-report-form", clear_on_submit=False):
            report_type = st.selectbox(
                "Report type",
                tuple(REPORT_LABELS),
                key="feedback_report_type",
            )
            short_title = st.text_input(
                "Short title",
                max_chars=200,
                key="feedback_short_title",
            )
            comment = st.text_area(
                "Detailed comment",
                max_chars=5_000,
                height=160,
                key="feedback_comment",
            )
            contact = st.text_input(
                "Name or contact (optional)",
                max_chars=500,
                key="feedback_contact",
            )
            include_state = st.checkbox(
                "Include the current app state",
                value=False,
                key="feedback_include_state",
                help=(
                    "Includes a size-limited diagnostic snapshot. Sensitive "
                    "keys, uploaded files, and binary values are removed."
                ),
            )
            submitted = st.form_submit_button(
                "Submit feedback",
                type="primary",
                disabled=in_progress,
                width="stretch",
            )

        if in_progress:
            st.info("A feedback report is already being submitted.")
            return
        if not submitted:
            previous = st.session_state.get("feedback_last_success")
            if isinstance(previous, Mapping):
                number = previous.get("number")
                url = previous.get("url")
                if isinstance(number, int) and isinstance(url, str):
                    st.success(
                        f"Created GitHub Issue [#{number}]({url})."
                    )
            return
        if not short_title.strip() or not comment.strip():
            st.error("Enter both a short title and a detailed comment.")
            return

        fingerprint_source = json.dumps(
            {
                "report_type": report_type,
                "short_title": short_title.strip(),
                "comment": comment.strip(),
                "contact": contact.strip(),
                "include_state": include_state,
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
        fingerprint = hashlib.sha256(fingerprint_source).hexdigest()
        previous = st.session_state.get("feedback_last_success")
        if (
            fingerprint
            == st.session_state.get("feedback_last_submission_fingerprint")
            and isinstance(previous, Mapping)
        ):
            number = previous.get("number")
            url = previous.get("url")
            if isinstance(number, int) and isinstance(url, str):
                st.success(f"Created GitHub Issue [#{number}]({url}).")
                return

        st.session_state["feedback_submission_in_progress"] = True
        st.session_state["feedback_last_submission_fingerprint"] = fingerprint
        st.session_state["feedback_last_success"] = None
        try:
            config = github_config_from_secrets(st.secrets)
            page_context = get_current_page_context(
                "Geo Stream Coastal Flood Explorer",
                dict(st.query_params),
            )
            snapshot = None
            if include_state:
                snapshot = sanitize_session_state(
                    dict(st.session_state),
                    excluded_prefixes=("feedback_",),
                )
            report_id = str(uuid4())
            submitted_at = _utc_now()
            try:
                current_url = st.context.url
            except Exception:
                current_url = None
            body = build_issue_body(
                comment=comment,
                contact=contact,
                report_type=report_type,
                submitted_at=submitted_at,
                current_page=str(page_context["current_page"]),
                query_parameters=page_context["query_parameters"],
                app_version=get_app_version(
                    repository_directory=Path(__file__).resolve().parent,
                ),
                deployment_environment=get_deployment_environment(
                    current_url=current_url,
                ),
                report_id=report_id,
                state_snapshot=snapshot,
            )
            with st.spinner("Submitting feedback to GitHub…"):
                issue = create_github_issue(
                    config,
                    title=format_issue_title(report_type, short_title),
                    body=body,
                    label=REPORT_LABELS[report_type],
                )
            st.session_state["feedback_last_success"] = {
                "number": issue.number,
                "url": issue.url,
            }
            st.success(
                f"Created GitHub Issue [#{issue.number}]({issue.url})."
            )
            if not issue.label_applied:
                st.caption(
                    "The issue was created without a label because the "
                    "configured repository did not accept that label."
                )
        except FeedbackError as exc:
            st.session_state["feedback_last_submission_fingerprint"] = None
            st.error(str(exc))
        except Exception:
            st.session_state["feedback_last_submission_fingerprint"] = None
            LOGGER.exception("Unexpected feedback submission failure")
            st.error(
                "The feedback report could not be submitted. Please try "
                "again later."
            )
        finally:
            st.session_state["feedback_submission_in_progress"] = False


def _render_sidebar() -> None:
    """Render the sidebar — CaSR-Land only; other layers are hidden for now."""

    bbox = _current_bbox()
    with st.sidebar:
        _render_feedback_form()
        st.divider()
        _render_casr_controls(bbox, st.session_state.get("active_roi"))


def _render_source_status(stale: bool) -> None:
    source_mode = st.session_state.get("current_source_mode")
    timestamp = st.session_state.get("fetch_timestamp")
    if source_mode is None:
        st.info(
            "Draw a region, choose a recent archive issue-date range, then "
            "explicitly fetch those ECCC forecast snapshots or generate "
            "synthetic test data."
        )
        return

    source_label = (
        "SYNTHETIC TEST DATA — NOT ECCC DATA"
        if source_mode == "synthetic"
        else "Recent ECCC forecast archive"
    )
    timestamp_text = (
        format_utc_datetime(timestamp) if isinstance(timestamp, datetime) else ""
    )
    timestamp_action = (
        "Generated" if source_mode == "synthetic" else "Retrieved"
    )
    st.caption(
        f"Displayed source: **{source_label}**"
        + (
            f" · {timestamp_action} {timestamp_text}"
            if timestamp_text
            else ""
        )
    )
    if source_mode == "archive":
        raw_count = int(st.session_state.get("raw_feature_count", 0))
        clipped_count = int(st.session_state.get("clipped_feature_count", 0))
        product_count = int(
            st.session_state.get("archive_product_count", 0)
        )
        requested_date_count = int(
            st.session_state.get("archive_requested_date_count", 0)
        )
        successful_date_count = int(
            st.session_state.get("archive_successful_date_count", 0)
        )
        loaded_range = st.session_state.get(
            "last_requested_archive_range"
        )
        loaded_range_text = _archive_range_text(loaded_range)
        st.success(
            f"Loaded ECCC archive range {loaded_range_text}"
            + (f" · Retrieved {timestamp_text}" if timestamp_text else "")
            + (
                f" · {successful_date_count}/{requested_date_count} "
                "issue date(s)"
            )
            + f" · {product_count} file(s) · {raw_count} feature(s) · "
            f"{clipped_count} intersected the exact region"
        )
        date_failures = st.session_state.get("archive_date_failures", [])
        if isinstance(date_failures, list) and date_failures:
            st.warning(
                f"{len(date_failures)} selected archive "
                f"{'date was' if len(date_failures) == 1 else 'dates were'} "
                "not loaded. The displayed range contains only the "
                "successfully loaded issue dates."
            )
            with st.expander("Archive dates not loaded"):
                for failure in date_failures:
                    if not isinstance(failure, Mapping):
                        continue
                    issue_date = str(
                        failure.get("issue_date", "unknown date")
                    )
                    message = str(
                        failure.get("message", "The date was not loaded.")
                    )
                    st.write(f"- {issue_date}: {message}")

        selected_range = _normalize_archive_range(
            st.session_state.get("selected_archive_range"),
            recent_archive_window(),
        )
        if (
            selected_range is not None
            and isinstance(loaded_range, (tuple, list))
            and tuple(loaded_range) != selected_range
        ):
            st.info(
                f"The range selector is now "
                f"{_archive_range_text(selected_range)}, but the displayed "
                f"results are still the loaded "
                f"{_archive_range_text(loaded_range)} range. Press fetch to "
                "replace them."
            )
    if stale:
        st.warning(
            "The current drawing differs from the ROI used for these results. "
            "Fetch or generate data again before downloading."
        )


def _render_results(
    filtered: dict[str, Any],
    *,
    stale: bool,
) -> None:
    source_mode = st.session_state.get("current_source_mode")
    raw_count = int(st.session_state.get("raw_feature_count", 0))
    clipped_count = int(st.session_state.get("clipped_feature_count", 0))
    summary = summarize_features(filtered)

    if source_mode == "archive" and raw_count == 0:
        st.info(
            "The successfully loaded dates in this archive range contained "
            "forecast files, but they published no coastal-flood-risk "
            "polygons. This is not an all-clear and does not describe "
            "observed flood history."
        )
    elif source_mode == "archive" and clipped_count == 0:
        st.info(
            "The loaded archive range contained coastal-flood-risk polygons, "
            "but none produced a usable intersection with the exact drawn "
            "region."
        )
    elif clipped_count > 0 and summary.feature_count == 0:
        st.info("No fetched features match the current filters.")
    elif source_mode == "synthetic":
        st.warning(
            "The displayed features are synthetic test data and are not an "
            "ECCC product or forecast."
        )

    warnings = st.session_state.get("clip_warnings", [])
    if warnings:
        st.warning(
            f"{len(warnings)} malformed or unusable feature(s) were skipped "
            "during local clipping."
        )
        with st.expander("Skipped-feature details"):
            for warning in warnings:
                st.write(f"- {warning}")

    st.subheader("Summary")
    first_row = st.columns(3)
    first_row[0].metric("Filtered features", summary.feature_count)
    first_row[1].metric(
        "Forecast validity range",
        _format_range(summary.earliest_validity, summary.latest_validity),
    )
    first_row[2].metric(
        "Publication-time range",
        _format_range(
            summary.earliest_publication,
            summary.latest_publication,
        ),
    )

    risk_columns = st.columns(len(RISK_LEVELS))
    for column, risk in zip(risk_columns, RISK_LEVELS, strict=True):
        column.metric(risk, summary.risk_counts.get(risk, 0))

    st.subheader("Feature table")
    table = feature_collection_to_dataframe(filtered)
    st.dataframe(
        table,
        width="stretch",
        hide_index=True,
    )

    download_name = export_filename()
    if source_mode == "synthetic":
        download_name = download_name.replace(
            "eccc_coastal_flooding_",
            "synthetic_test_coastal_flooding_",
        )
    elif source_mode == "archive":
        requested_count = int(
            st.session_state.get("archive_requested_date_count", 0)
        )
        successful_count = int(
            st.session_state.get("archive_successful_date_count", 0)
        )
        download_name = _archive_clipped_filename(
            download_name,
            st.session_state.get("last_requested_archive_range"),
            requested_count,
            successful_count,
        )
    download_columns = st.columns(2 if source_mode == "archive" else 1)
    download_columns[0].download_button(
        "Download clipped GeoJSON",
        data=feature_collection_bytes(filtered),
        file_name=download_name,
        mime="application/geo+json",
        disabled=st.session_state.get("clipped_data") is None or stale,
        help=(
            "Fetch or generate results for the current ROI first."
            if stale
            else None
        ),
        width="stretch",
    )
    if source_mode == "archive":
        date_stamp = _archive_range_stamp(
            st.session_state.get("last_requested_archive_range")
        )
        download_columns[1].download_button(
            "Download raw fetched ECCC JSON",
            data=st.session_state.get("raw_archive_download") or b"{}",
            file_name=f"eccc_archive_raw_{date_stamp}.json",
            mime="application/json",
            disabled=st.session_state.get("raw_archive_download") is None,
            help=(
                "The decoded per-file ECCC responses, requested date range, "
                "and any not-loaded date diagnostics bundled before ROI "
                "clipping or filters."
            ),
            width="stretch",
        )


def _render_animation(
    criteria: FilterCriteria,
    *,
    stale: bool,
) -> None:
    """Render an optional timeline from already-fetched archive features."""

    if (
        stale
        or st.session_state.get("current_source_mode") != "archive"
        or st.session_state.get("clipped_data") is None
    ):
        return

    animation_criteria = FilterCriteria(
        validity=ALL_FORECAST_PERIODS,
        risks=criteria.risks,
        tide=criteria.tide,
        storm_surge=criteria.storm_surge,
        waves=criteria.waves,
    )
    animation_data = filter_features(
        st.session_state.get("clipped_data"),
        animation_criteria,
    )
    issuances = publication_times(animation_data)
    if not issuances:
        st.caption(
            "An animation becomes available when matching archive features "
            "include a valid forecast publication time."
        )
        return

    range_key = _archive_range_stamp(
        st.session_state.get("last_requested_archive_range")
    )
    issuance_key = f"forecast-animation-issuance-{range_key}"
    issuance_options = tuple(reversed(issuances))
    if st.session_state.get(issuance_key) not in issuance_options:
        st.session_state[issuance_key] = issuance_options[0]
    selected_issuance = st.selectbox(
        "Forecast issuance (UTC)",
        issuance_options,
        key=issuance_key,
        format_func=format_utc_datetime,
        help=(
            "Each animation contains exactly one published forecast issuance "
            "so forecasts from different issue times are never overlaid."
        ),
    )
    issuance_data = filter_by_publication_time(
        animation_data,
        selected_issuance,
    )
    try:
        timeline_data = prepare_timeline_data(issuance_data)
    except AnimationError:
        return

    if timeline_data.frame_count < 2:
        st.caption(
            "An animation becomes available when at least two forecast "
            "validity times intersect the region and match the non-time "
            "filters."
        )
        return

    st.subheader("Forecast animation")
    st.caption(
        f"Animating validity times from the forecast published "
        f"{format_utc_datetime(selected_issuance)}. Other issuances in the "
        "loaded date range are excluded, and no averaging is performed."
    )
    if not st.toggle(
        "Show forecast animation",
        value=False,
        key="show_forecast_animation",
    ):
        return

    try:
        animation_map = build_forecast_animation(
            issuance_data,
            roi=st.session_state.get("last_requested_roi"),
        )
    except AnimationError as exc:
        st.warning(str(exc))
        return

    issuance_stamp = selected_issuance.strftime("%Y%m%dT%H%M%SZ")
    st_folium(
        animation_map,
        key=f"forecast-animation-{range_key}-{issuance_stamp}",
        height=520,
        use_container_width=True,
        returned_objects=[],
    )


def main() -> None:
    """Render the application (CaSR-Land only for now)."""

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    st.set_page_config(
        page_title="Geo Stream — CaSR-Land",
        page_icon="🌊",
        layout="wide",
    )
    _initialize_state()

    st.title("Geo Stream — CaSR-Land")
    st.markdown(f"[View the Geo Stream repository on GitHub]({REPOSITORY_URL})")
    st.caption(
        "Draw a Canadian region, then fetch ECCC CaSR-Land v2.1 reanalysis "
        "for that exact shape. Other coastal layers (CHS gauges, ECCC flood "
        "polygons, GDSPS/RESPS) are temporarily hidden from this UI."
    )
    st.warning(
        "Exploratory visualization only. CaSR-Land is historical surface "
        "reanalysis (through 2017-12), not a warning service, inundation map, "
        "or live coastal forecast. Official ECCC weather alerts and emergency "
        "guidance take precedence."
    )

    _render_sidebar()

    st.subheader("Draw your region in Canada")
    st.info(
        "The map stays focused on Canada with extra room around its edges. "
        "Pan and zoom to any Canadian coast or northern area, then use the "
        "drawing toolbar.  \n"
        "**Rectangle:** choose the square button in the map's upper-left "
        "toolbar, then click, drag, and release.  \n"
        "**Polygon:** choose the polygon button, click each corner, then click "
        "the first point again to finish.  \n"
        "To change a region, choose the pencil or trash button, make the edit, "
        "then choose **Save**."
    )
    base_map = build_base_map()
    drawing_layer = build_drawing_hydration_layer(
        st.session_state.get("drawings", [])
    )
    if build_casr_overlay_layer is not None:
        casr_layer = build_casr_overlay_layer(
            st.session_state.get("casr_overlay_params"),
            enabled=bool(st.session_state.get("casr_enabled")),
        )
    else:
        import folium

        casr_layer = folium.FeatureGroup(
            name="CaSR-Land (unavailable)",
            control=True,
            show=False,
        )
    map_payload = st_folium(
        base_map,
        key=MAP_COMPONENT_KEY,
        height=650,
        use_container_width=True,
        returned_objects=MAP_RETURNED_OBJECTS,
        feature_group_to_add=[
            drawing_layer,
            casr_layer,
        ],
        layer_control=build_layer_control(),
        on_change=_sync_map_drawings,
    )
    # Custom-component callbacks normally update state before this rerun.
    # Applying the returned payload as a fallback closes the delete-then-draw
    # race where the callback can momentarily expose the preceding empty list.
    if _apply_map_drawings_payload(map_payload):
        st.rerun()
    st.markdown(
        (
            "<div id='geo-stream-map-scroll-space' aria-hidden='true' "
            "style='height:38vh;min-height:280px'></div>"
        ),
        unsafe_allow_html=True,
    )

    _render_casr_results()



if __name__ == "__main__":
    main()
