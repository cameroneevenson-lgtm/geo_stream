"""Streamlit entry point for the Geo Stream coastal flood explorer."""

from __future__ import annotations

import copy
import logging
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

import folium
import streamlit as st
from streamlit_folium import st_folium

from coastal_flood_explorer.api import ECCC_API_URL, ECCCClient, ECCCError
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
    roi_bbox,
)
from coastal_flood_explorer.map_view import (
    DEFAULT_CENTER,
    DEFAULT_ZOOM,
    build_base_map,
    build_result_layer,
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
    reconcile_drawings,
    roi_matches,
    viewport_from_map_payload,
)
from coastal_flood_explorer.synthetic import generate_synthetic_data


LOGGER = logging.getLogger("geo_stream.app")
MAP_COMPONENT_KEY = "coastal-flood-map"
EMPTY_COLLECTION = {"type": "FeatureCollection", "features": []}
STATE_DEFAULTS: dict[str, Any] = {
    "drawings": [],
    "active_roi": None,
    "drawing_warnings": [],
    "last_successful_live_response": None,
    "clipped_data": None,
    "last_requested_bbox": None,
    "last_requested_roi": None,
    "fetch_timestamp": None,
    "current_source_mode": None,
    "clip_warnings": [],
    "raw_feature_count": 0,
    "clipped_feature_count": 0,
    "map_center": DEFAULT_CENTER,
    "map_zoom": DEFAULT_ZOOM,
}


@st.cache_data(ttl=300, max_entries=32, show_spinner=False)
def _cached_fetch(
    api_url: str,
    language: str,
    rounded_bbox: tuple[float, float, float, float],
) -> dict[str, Any]:
    """Fetch public ECCC data using only stable, serializable cache keys."""

    return ECCCClient(api_url=api_url).fetch(rounded_bbox, language)


def _initialize_state() -> None:
    for key, value in STATE_DEFAULTS.items():
        if key not in st.session_state:
            st.session_state[key] = copy.deepcopy(value)


def _sync_map_drawings() -> None:
    """Synchronize the component's authoritative drawing collection."""

    payload = st.session_state.get(MAP_COMPONENT_KEY)
    if not isinstance(payload, Mapping):
        return
    center, zoom = viewport_from_map_payload(payload)
    if center is not None:
        st.session_state["map_center"] = center
    if zoom is not None:
        st.session_state["map_zoom"] = zoom

    drawings_value = payload.get("all_drawings")
    if drawings_value is None:
        return
    reconciled = reconcile_drawings(drawings_value)
    st.session_state["drawings"] = list(reconciled.drawings)
    st.session_state["active_roi"] = reconciled.active_roi
    st.session_state["drawing_warnings"] = list(reconciled.warnings)


def _rounded_bbox(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    return tuple(round(value, 6) for value in bbox)  # type: ignore[return-value]


def _store_dataset(
    *,
    raw_response: dict[str, Any],
    clipped_data: dict[str, Any],
    bbox: tuple[float, float, float, float],
    roi: Mapping[str, Any],
    source_mode: str,
    warnings: tuple[str, ...],
) -> None:
    if source_mode == "live":
        st.session_state["last_successful_live_response"] = raw_response
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


def _render_sidebar() -> tuple[FilterCriteria, str | None]:
    action_error: str | None = None
    bbox = _current_bbox()

    with st.sidebar:
        st.header("Region and data")
        if bbox is None:
            st.info("Draw a polygon or rectangle to select a region.")
        else:
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

        synthetic_enabled = st.checkbox(
            "Use synthetic test data",
            value=False,
            key="use_synthetic_test_data",
            help="Synthetic features are generated locally and are not ECCC data.",
        )

        fetch_live = st.button(
            "Fetch ECCC data",
            type="primary",
            disabled=bbox is None,
            width="stretch",
        )
        generate_synthetic = False
        if synthetic_enabled:
            generate_synthetic = st.button(
                "Generate synthetic test data",
                disabled=bbox is None,
                width="stretch",
            )

        if fetch_live and bbox is not None:
            active_roi = st.session_state.get("active_roi")
            try:
                with st.spinner("Fetching ECCC coastal-flood data…"):
                    live_response = _cached_fetch(
                        ECCC_API_URL,
                        "en",
                        _rounded_bbox(bbox),
                    )
                    clipped = clip_feature_collection(live_response, active_roi)
                _store_dataset(
                    raw_response=live_response,
                    clipped_data=clipped.feature_collection,
                    bbox=bbox,
                    roi=active_roi,
                    source_mode="live",
                    warnings=clipped.warnings,
                )
            except (ECCCError, GeometryError) as exc:
                LOGGER.warning("Live fetch failed: %s", exc, exc_info=True)
                action_error = str(exc)
            except Exception:
                LOGGER.exception("Unexpected failure while fetching ECCC data")
                action_error = (
                    "An unexpected error occurred while processing the ECCC "
                    "response. The previous results were kept."
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
            "Draw a region, then explicitly fetch ECCC data or generate "
            "synthetic test data."
        )
        return

    source_label = (
        "SYNTHETIC TEST DATA — NOT ECCC DATA"
        if source_mode == "synthetic"
        else "ECCC GeoMet"
    )
    timestamp_text = (
        format_utc_datetime(timestamp) if isinstance(timestamp, datetime) else ""
    )
    st.caption(
        f"Displayed source: **{source_label}**"
        + (f" · Retrieved/generated {timestamp_text}" if timestamp_text else "")
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

    if source_mode == "live" and raw_count == 0:
        st.info(
            "No active coastal-flooding polygons were returned for this region."
        )
    elif source_mode == "live" and clipped_count == 0:
        st.info(
            "ECCC returned bounding-box features, but none produced a usable "
            "polygon intersection with the exact drawn region."
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
    st.download_button(
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
        "Draw a coastal region, fetch ECCC polygons for its bounding box, "
        "then explore the results clipped to the exact drawing."
    )
    st.warning(
        "Exploratory visualization only. Official ECCC weather alerts and "
        "emergency guidance take precedence."
    )

    criteria, action_error = _render_sidebar()
    if action_error:
        st.error(action_error)

    clipped_data = st.session_state.get("clipped_data")
    filtered = filter_features(clipped_data, criteria)
    stale = _results_are_stale()
    _render_source_status(stale)

    synthetic = st.session_state.get("current_source_mode") == "synthetic"
    base_map = build_base_map(
        st.session_state.get("drawings", []),
        synthetic=synthetic,
    )
    result_layer = build_result_layer(filtered, synthetic=synthetic)
    st_folium(
        base_map,
        key=MAP_COMPONENT_KEY,
        height=650,
        use_container_width=True,
        returned_objects=["all_drawings", "bounds", "zoom"],
        center=tuple(st.session_state.get("map_center", DEFAULT_CENTER)),
        zoom=int(st.session_state.get("map_zoom", DEFAULT_ZOOM)),
        feature_group_to_add=result_layer,
        layer_control=folium.LayerControl(collapsed=False),
        on_change=_sync_map_drawings,
    )

    _render_results(filtered, stale=stale)


if __name__ == "__main__":
    main()
