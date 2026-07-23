"""Pure filtering and summary helpers for clipped GeoJSON features."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .properties import (
    PUBLICATION_PROPERTY,
    RISK_LEVELS,
    RISK_PROPERTY,
    STORM_SURGE_PROPERTY,
    TIDE_PROPERTY,
    VALIDITY_PROPERTY,
    WAVES_PROPERTY,
    format_utc_datetime,
    get_property,
    normalize_contributor,
    normalize_risk,
    parse_utc_datetime,
)

ALL_FORECAST_PERIODS = "All forecast periods"
UNKNOWN_FORECAST_PERIOD = "Unknown / missing"
ANY_CONTRIBUTOR = "Any"


@dataclass(frozen=True, slots=True)
class FilterCriteria:
    """Current feature filters.

    An empty risk selection intentionally matches no features, mirroring a
    cleared Streamlit multiselect. Contributor values are ``Any``, ``Yes``,
    ``No``, or ``Unknown``.
    """

    validity: str = ALL_FORECAST_PERIODS
    risks: tuple[str, ...] = field(default_factory=lambda: RISK_LEVELS)
    tide: str = ANY_CONTRIBUTOR
    storm_surge: str = ANY_CONTRIBUTOR
    waves: str = ANY_CONTRIBUTOR


@dataclass(frozen=True, slots=True)
class FeatureSummary:
    """Summary statistics for a filtered feature set."""

    feature_count: int
    risk_counts: dict[str, int]
    earliest_validity: datetime | None
    latest_validity: datetime | None
    earliest_publication: datetime | None
    latest_publication: datetime | None

    @property
    def validity_range(self) -> tuple[datetime | None, datetime | None]:
        """Return the earliest and latest forecast validity datetimes."""

        return self.earliest_validity, self.latest_validity

    @property
    def publication_range(self) -> tuple[datetime | None, datetime | None]:
        """Return the earliest and latest publication datetimes."""

        return self.earliest_publication, self.latest_publication

    def as_dict(self) -> dict[str, Any]:
        """Return a serializable summary with UTC ISO datetime strings."""

        return {
            "feature_count": self.feature_count,
            "risk_counts": dict(self.risk_counts),
            "earliest_validity": format_utc_datetime(self.earliest_validity) or None,
            "latest_validity": format_utc_datetime(self.latest_validity) or None,
            "earliest_publication": (
                format_utc_datetime(self.earliest_publication) or None
            ),
            "latest_publication": (
                format_utc_datetime(self.latest_publication) or None
            ),
        }


def validity_option(value: Any) -> str:
    """Return the canonical selector option for a validity value."""

    return format_utc_datetime(value) or UNKNOWN_FORECAST_PERIOD


def _feature_sequence(
    feature_collection: Mapping[str, Any] | None,
) -> list[Mapping[str, Any]]:
    if not isinstance(feature_collection, Mapping):
        return []
    features = feature_collection.get("features")
    if not isinstance(features, Sequence) or isinstance(
        features, (str, bytes, bytearray)
    ):
        return []
    return [feature for feature in features if isinstance(feature, Mapping)]


def forecast_period_options(
    feature_collection: Mapping[str, Any] | None,
) -> list[str]:
    """Return sorted selector options, including unknown when represented."""

    known: dict[str, datetime] = {}
    has_unknown = False
    for feature in _feature_sequence(feature_collection):
        properties = feature.get("properties")
        props = properties if isinstance(properties, Mapping) else {}
        raw_value = get_property(props, VALIDITY_PROPERTY)
        parsed = parse_utc_datetime(raw_value)
        if parsed is None:
            has_unknown = True
        else:
            known[format_utc_datetime(parsed)] = parsed

    options = [ALL_FORECAST_PERIODS]
    options.extend(key for key, _ in sorted(known.items(), key=lambda item: item[1]))
    if has_unknown:
        options.append(UNKNOWN_FORECAST_PERIOD)
    return options


def _canonical_risk_selection(values: Iterable[Any]) -> set[str]:
    selected: set[str] = set()
    for value in values:
        if isinstance(value, str) and value.strip().casefold() == "unknown":
            selected.add("Unknown")
            continue
        normalized = normalize_risk(value)
        if normalized != "Unknown" or value in RISK_LEVELS:
            selected.add(normalized)
    return selected


def feature_matches(
    feature: Mapping[str, Any],
    criteria: FilterCriteria,
) -> bool:
    """Return whether one feature matches all filter criteria."""

    properties = feature.get("properties")
    props = properties if isinstance(properties, Mapping) else {}

    selected_risks = _canonical_risk_selection(criteria.risks)
    if normalize_risk(get_property(props, RISK_PROPERTY)) not in selected_risks:
        return False

    if criteria.validity != ALL_FORECAST_PERIODS:
        if validity_option(get_property(props, VALIDITY_PROPERTY)) != criteria.validity:
            return False

    contributor_checks = (
        (criteria.tide, TIDE_PROPERTY),
        (criteria.storm_surge, STORM_SURGE_PROPERTY),
        (criteria.waves, WAVES_PROPERTY),
    )
    for selected_value, path in contributor_checks:
        if selected_value != ANY_CONTRIBUTOR:
            if normalize_contributor(get_property(props, path)) != selected_value:
                return False

    return True


def filter_features(
    feature_collection: Mapping[str, Any] | None,
    criteria: FilterCriteria,
) -> dict[str, Any]:
    """Return a fresh FeatureCollection containing only matching features."""

    return {
        "type": "FeatureCollection",
        "features": [
            feature
            for feature in _feature_sequence(feature_collection)
            if feature_matches(feature, criteria)
        ],
    }


def _datetime_values(
    features: Iterable[Mapping[str, Any]],
    path: str,
) -> list[datetime]:
    parsed_values: list[datetime] = []
    for feature in features:
        properties = feature.get("properties")
        props = properties if isinstance(properties, Mapping) else {}
        parsed = parse_utc_datetime(get_property(props, path))
        if parsed is not None:
            parsed_values.append(parsed)
    return parsed_values


def summarize_features(
    feature_collection: Mapping[str, Any] | None,
) -> FeatureSummary:
    """Compute feature count, risk counts, and available datetime ranges."""

    features = _feature_sequence(feature_collection)
    observed = Counter()
    for feature in features:
        properties = feature.get("properties")
        props = properties if isinstance(properties, Mapping) else {}
        observed[normalize_risk(get_property(props, RISK_PROPERTY))] += 1
    risk_counts = {risk: observed.get(risk, 0) for risk in RISK_LEVELS}

    validity_values = _datetime_values(features, VALIDITY_PROPERTY)
    publication_values = _datetime_values(features, PUBLICATION_PROPERTY)
    return FeatureSummary(
        feature_count=len(features),
        risk_counts=risk_counts,
        earliest_validity=min(validity_values) if validity_values else None,
        latest_validity=max(validity_values) if validity_values else None,
        earliest_publication=min(publication_values) if publication_values else None,
        latest_publication=max(publication_values) if publication_values else None,
    )


# Concise aliases used by the Streamlit orchestration layer.
apply_filters = filter_features
build_forecast_options = forecast_period_options
summarize = summarize_features
