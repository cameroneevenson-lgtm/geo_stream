"""Streamlit entry point for the Geo Stream coastal flood explorer."""

from __future__ import annotations

import copy
import logging
from collections.abc import Mapping
from datetime import date, datetime, timezone
from typing import Any
from urllib.parse import quote

import streamlit as st
from streamlit_folium import st_folium

from coastal_flood_explorer.animation import (
    AnimationError,
    build_forecast_animation,
    prepare_timeline_data,
)
from coastal_flood_explorer.archive import (
    ARCHIVE_BASE_URL,
    ArchiveError,
    ArchiveFetchResult,
    ECCCDatamartArchiveClient,
    raw_bundle_bytes,
)
from coastal_flood_explorer.archive_dates import recent_archive_window
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
from coastal_flood_explorer.geometry import (
    GeometryError,
    clip_feature_collection,
    rank_points_for_roi,
    roi_bbox,
)
from coastal_flood_explorer.map_view import (
    build_base_map,
    build_chs_station_layer,
    build_drawing_hydration_layer,
    build_layer_control,
    build_result_layer,
    risk_legend_html,
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
MAP_COMPONENT_KEY = "coastal-flood-map-v5"
EMPTY_COLLECTION = {"type": "FeatureCollection", "features": []}
STATE_DEFAULTS: dict[str, Any] = {
    "drawings": [],
    "active_roi": None,
    "drawing_warnings": [],
    "last_successful_archive_response": None,
    "clipped_data": None,
    "last_requested_bbox": None,
    "last_requested_roi": None,
    "last_requested_archive_date": None,
    "fetch_timestamp": None,
    "current_source_mode": None,
    "clip_warnings": [],
    "raw_feature_count": 0,
    "clipped_feature_count": 0,
    "archive_product_count": 0,
    "raw_archive_download": None,
    "chs_catalog": None,
    "chs_bundles": {},
    "chs_selection_roi": None,
    "selected_chs_station_id": None,
}


@st.cache_data(ttl=300, max_entries=128, show_spinner=False)
def _cached_archive_fetch(
    archive_root: str,
    archive_date: str,
) -> ArchiveFetchResult:
    """Fetch one archive issue using only stable, serializable cache keys."""

    return ECCCDatamartArchiveClient(
        archive_root=archive_root,
    ).fetch_date(archive_date)


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


def _initialize_state() -> None:
    for key, value in STATE_DEFAULTS.items():
        if key not in st.session_state:
            st.session_state[key] = copy.deepcopy(value)


def _utc_now() -> datetime:
    """Return the current aware UTC time through a testable seam."""

    return datetime.now(timezone.utc)


def _sync_map_drawings() -> None:
    """Synchronize the component's authoritative drawing collection."""

    payload = st.session_state.get(MAP_COMPONENT_KEY)
    if not isinstance(payload, Mapping):
        return
    drawings_value = payload.get("all_drawings")
    if drawings_value is None:
        return
    reconciled = reconcile_drawings(drawings_value)
    st.session_state["drawings"] = list(reconciled.drawings)
    st.session_state["active_roi"] = reconciled.active_roi
    st.session_state["drawing_warnings"] = list(reconciled.warnings)


def _store_dataset(
    *,
    raw_response: dict[str, Any],
    clipped_data: dict[str, Any],
    bbox: tuple[float, float, float, float],
    roi: Mapping[str, Any],
    source_mode: str,
    warnings: tuple[str, ...],
    archive_date: date | None = None,
    archive_product_count: int = 0,
    raw_archive_download: bytes | None = None,
) -> None:
    if source_mode == "archive":
        st.session_state["last_successful_archive_response"] = raw_response
        st.session_state["last_requested_archive_date"] = archive_date
        st.session_state["archive_product_count"] = archive_product_count
        st.session_state["raw_archive_download"] = raw_archive_download
    st.session_state["clipped_data"] = clipped_data
    st.session_state["last_requested_bbox"] = bbox
    st.session_state["last_requested_roi"] = copy.deepcopy(dict(roi))
    st.session_state["fetch_timestamp"] = datetime.now(timezone.utc)
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


def _render_sidebar() -> tuple[FilterCriteria, str | None]:
    action_error: str | None = None
    bbox = _current_bbox()
    archive_window = recent_archive_window()

    with st.sidebar:
        st.header("ECCC forecast overlay")
        if bbox is None:
            st.info(
                "No region selected yet. Use the polygon or rectangle button "
                "in the map's upper-left drawing toolbar to draw within Canada."
            )
        else:
            st.success("Region selected — the data actions are ready.")
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

        archive_date = st.date_input(
            "Archived ECCC issue date (UTC)",
            value=archive_window.default,
            min_value=archive_window.oldest,
            max_value=archive_window.newest,
            key="selected_archive_date",
            help=(
                "ECCC currently retains about 30 days of forecast files. "
                "These are archived forecasts, not observed flood history."
            ),
        )
        st.caption(
            "Recent ECCC forecast archive (up to 30 days). Today's issue may "
            "still be publishing. Changing this date does not contact ECCC "
            "until you press the fetch button."
        )
        fetch_archive = st.button(
            "Fetch archived ECCC forecast",
            type="primary",
            disabled=bbox is None,
            width="stretch",
        )

        synthetic_enabled = st.checkbox(
            "Use synthetic test data",
            value=False,
            key="use_synthetic_test_data",
            help="Synthetic features are generated locally and are not ECCC data.",
        )

        generate_synthetic = False
        if synthetic_enabled:
            generate_synthetic = st.button(
                "Generate synthetic test data",
                disabled=bbox is None,
                width="stretch",
            )

        if fetch_archive and bbox is not None:
            active_roi = st.session_state.get("active_roi")
            requested_date = (
                archive_date
                if isinstance(archive_date, date)
                else archive_window.default
            )
            fetch_progress = st.status(
                "Fetching archived ECCC coastal-flood forecasts…",
                expanded=True,
                state="running",
            )
            try:
                with fetch_progress:
                    st.write(
                        "Contacting the official ECCC Datamart archive for "
                        f"{requested_date.isoformat()}…"
                    )
                    archive_result = _cached_archive_fetch(
                        ARCHIVE_BASE_URL,
                        requested_date.strftime("%Y%m%d"),
                    )
                    st.write(
                        f"Validated {len(archive_result.products)} archived "
                        "forecast file(s)."
                    )
                    st.write(
                        "Clipping their polygons locally to the exact region…"
                    )
                    clipped = clip_feature_collection(
                        archive_result.collection,
                        active_roi,
                    )
                _store_dataset(
                    raw_response=archive_result.collection,
                    clipped_data=clipped.feature_collection,
                    bbox=bbox,
                    roi=active_roi,
                    source_mode="archive",
                    warnings=clipped.warnings,
                    archive_date=requested_date,
                    archive_product_count=len(archive_result.products),
                    raw_archive_download=raw_bundle_bytes(
                        archive_result,
                        requested_date,
                    ),
                )
                fetch_progress.update(
                    label=(
                        "ECCC archive fetch complete — "
                        f"{len(archive_result.products)} file(s), "
                        f"{st.session_state['raw_feature_count']} feature(s), "
                        f"{st.session_state['clipped_feature_count']} intersected "
                        "the region"
                    ),
                    state="complete",
                    expanded=False,
                )
            except (ArchiveError, GeometryError) as exc:
                LOGGER.warning("Archive fetch failed: %s", exc, exc_info=True)
                action_error = str(exc)
                fetch_progress.update(
                    label=(
                        "ECCC archive fetch failed — previous results were kept"
                    ),
                    state="error",
                    expanded=True,
                )
            except Exception:
                LOGGER.exception(
                    "Unexpected failure while fetching the ECCC archive"
                )
                action_error = (
                    "An unexpected error occurred while processing the ECCC "
                    "archive. The previous results were kept."
                )
                fetch_progress.update(
                    label=(
                        "ECCC archive fetch failed — previous results were kept"
                    ),
                    state="error",
                    expanded=True,
                )

        if generate_synthetic and bbox is not None:
            active_roi = st.session_state.get("active_roi")
            try:
                generated = generate_synthetic_data(active_roi)
                clipped = clip_feature_collection(generated, active_roi)
                _store_dataset(
                    raw_response=generated,
                    clipped_data=clipped.feature_collection,
                    bbox=bbox,
                    roi=active_roi,
                    source_mode="synthetic",
                    warnings=clipped.warnings,
                )
            except GeometryError as exc:
                LOGGER.warning(
                    "Synthetic generation failed: %s",
                    exc,
                    exc_info=True,
                )
                action_error = str(exc)
            except Exception:
                LOGGER.exception("Unexpected synthetic-data failure")
                action_error = (
                    "Synthetic test data could not be generated for this ROI."
                )

        clipped_data = st.session_state.get("clipped_data")
        options = forecast_period_options(clipped_data)
        selected_validity = st.session_state.get(
            "filter_validity",
            ALL_FORECAST_PERIODS,
        )
        if selected_validity not in options:
            st.session_state["filter_validity"] = ALL_FORECAST_PERIODS

        st.divider()
        st.header("Filters")
        validity = st.selectbox(
            "Forecast validity time",
            options,
            key="filter_validity",
        )
        risks = st.multiselect(
            "Risk level",
            list(RISK_LEVELS),
            default=list(RISK_LEVELS),
            key="filter_risks",
        )
        tide = st.selectbox(
            "Tide contribution",
            CONTRIBUTOR_VALUES,
            key="filter_tide",
        )
        storm_surge = st.selectbox(
            "Storm-surge contribution",
            CONTRIBUTOR_VALUES,
            key="filter_storm_surge",
        )
        waves = st.selectbox(
            "Wave contribution",
            CONTRIBUTOR_VALUES,
            key="filter_waves",
        )

    return (
        FilterCriteria(
            validity=validity,
            risks=tuple(risks),
            tide=tide,
            storm_surge=storm_surge,
            waves=waves,
        ),
        action_error,
    )


def _render_source_status(stale: bool) -> None:
    source_mode = st.session_state.get("current_source_mode")
    timestamp = st.session_state.get("fetch_timestamp")
    if source_mode is None:
        st.info(
            "Draw a region, choose a recent archive issue date, then "
            "explicitly fetch the ECCC forecast or generate synthetic test "
            "data."
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
        loaded_date = st.session_state.get("last_requested_archive_date")
        loaded_date_text = (
            loaded_date.isoformat()
            if isinstance(loaded_date, date)
            else "unknown date"
        )
        st.success(
            f"Loaded ECCC archive issue {loaded_date_text}"
            + (f" · Retrieved {timestamp_text}" if timestamp_text else "")
            + f" · {product_count} file(s) · {raw_count} feature(s) · "
            f"{clipped_count} intersected the exact region"
        )
        selected_date = st.session_state.get("selected_archive_date")
        if (
            isinstance(loaded_date, date)
            and isinstance(selected_date, date)
            and loaded_date != selected_date
        ):
            st.info(
                f"The date selector is now {selected_date.isoformat()}, but "
                f"the displayed results are still the loaded "
                f"{loaded_date.isoformat()} issue. Press fetch to replace "
                "them."
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
            "The selected archived issue contained forecast files, but they "
            "published no coastal-flood-risk polygons. This is not an "
            "all-clear and does not describe observed flood history."
        )
    elif source_mode == "archive" and clipped_count == 0:
        st.info(
            "The archived issue contained coastal-flood-risk polygons, but "
            "none produced a usable intersection with the exact drawn region."
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
        loaded_date = st.session_state.get("last_requested_archive_date")
        date_stamp = (
            loaded_date.strftime("%Y%m%d")
            if isinstance(loaded_date, date)
            else "unknown"
        )
        download_columns[1].download_button(
            "Download raw fetched ECCC JSON",
            data=st.session_state.get("raw_archive_download") or b"{}",
            file_name=f"eccc_archive_raw_{date_stamp}.json",
            mime="application/json",
            disabled=st.session_state.get("raw_archive_download") is None,
            help=(
                "The decoded per-file ECCC responses bundled before ROI "
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
    try:
        timeline_data = prepare_timeline_data(animation_data)
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
        "This animates validity times within the loaded archived issue. It "
        "does not average or automatically download all 30 retained days."
    )
    if not st.toggle(
        "Show forecast animation",
        value=False,
        key="show_forecast_animation",
    ):
        return

    try:
        animation_map = build_forecast_animation(
            animation_data,
            roi=st.session_state.get("last_requested_roi"),
        )
    except AnimationError as exc:
        st.warning(str(exc))
        return

    loaded_date = st.session_state.get("last_requested_archive_date")
    date_key = (
        loaded_date.strftime("%Y%m%d")
        if isinstance(loaded_date, date)
        else "unknown"
    )
    st_folium(
        animation_map,
        key=f"forecast-animation-{date_key}",
        height=520,
        use_container_width=True,
        returned_objects=[],
    )


def main() -> None:
    """Render the application."""

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    st.set_page_config(
        page_title="Geo Stream Coastal Flood Explorer",
        page_icon="🌊",
        layout="wide",
    )
    _initialize_state()

    st.title("Geo Stream Coastal Flood Explorer")
    st.caption(
        "Draw any Canadian region to automatically select an official CHS "
        "water-level gauge inside it, or the nearest gauge when none lies "
        "inside. ECCC coastal-flood forecast polygons remain an optional, "
        "explicit fetch."
    )
    st.warning(
        "Exploratory visualization only. CHS station measurements are not "
        "inundation maps. Official ECCC weather alerts and emergency guidance "
        "take precedence."
    )
    st.caption(
        "The ECCC archive contains recent forecasts—not observed floods, a "
        "30-day average, or permanent hazard mapping."
    )

    chs_stations, selected_chs_station_id, chs_bundle = (
        _render_chs_water_levels()
    )
    criteria, action_error = _render_sidebar()
    if action_error:
        st.error(action_error)

    clipped_data = st.session_state.get("clipped_data")
    filtered = filter_features(clipped_data, criteria)
    stale = _results_are_stale()
    _render_source_status(stale)

    synthetic = st.session_state.get("current_source_mode") == "synthetic"
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
    st.markdown(risk_legend_html(), unsafe_allow_html=True)
    st.caption(
        "Blue dots are operating CHS observation stations. The larger, darker "
        "dot is the station shown in the water-level chart; use the map layer "
        "control to hide or show the station layer."
    )
    base_map = build_base_map()
    drawing_layer = build_drawing_hydration_layer(
        st.session_state.get("drawings", [])
    )
    result_layer = build_result_layer(filtered, synthetic=synthetic)
    chs_station_layer = build_chs_station_layer(
        chs_stations,
        selected_station_id=selected_chs_station_id,
        bundle=chs_bundle,
    )
    st_folium(
        base_map,
        key=MAP_COMPONENT_KEY,
        height=650,
        use_container_width=True,
        returned_objects=MAP_RETURNED_OBJECTS,
        feature_group_to_add=[
            drawing_layer,
            result_layer,
            chs_station_layer,
        ],
        layer_control=build_layer_control(),
        on_change=_sync_map_drawings,
    )

    _render_animation(criteria, stale=stale)
    _render_results(filtered, stale=stale)


if __name__ == "__main__":
    main()
