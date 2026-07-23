"""Property access, normalization, display, table, and export helpers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from decimal import Decimal
from html import escape
import json
import math
from pathlib import Path
from typing import Any

import pandas as pd

RISK_PROPERTY = "metobject.risk.value"
IMPACT_PROPERTY = "metobject.impact.value"
LIKELIHOOD_PROPERTY = "metobject.likelihood.value"
TIDE_PROPERTY = "metobject.tide.value"
STORM_SURGE_PROPERTY = "metobject.storm_surge.value"
WAVES_PROPERTY = "metobject.waves.value"
VALIDITY_PROPERTY = "validity_datetime"
PUBLICATION_PROPERTY = "publication_datetime"
EXPIRATION_PROPERTY = "expiration_datetime"

RISK_LEVELS: tuple[str, ...] = (
    "Low",
    "Moderate",
    "High",
    "Extreme",
    "Unknown",
)
RISK_ORDER = {label: index for index, label in enumerate(RISK_LEVELS)}
RISK_COLOURS = {
    "Low": "#808080",
    "Moderate": "#f2d024",
    "High": "#f28e2b",
    "Extreme": "#d62728",
    "Unknown": "#607d8b",
}
CONTRIBUTOR_VALUES: tuple[str, ...] = ("Any", "Yes", "No", "Unknown")

TABLE_COLUMNS: tuple[str, ...] = (
    "feature_id",
    "risk",
    "impact",
    "likelihood",
    "tide",
    "storm_surge",
    "waves",
    "validity_datetime",
    "publication_datetime",
    "expiration_datetime",
    "amendment",
    "domain",
    "status",
    "file_id",
    "source",
)

_MISSING = object()
_RISK_NUMBERS = {1: "Low", 2: "Moderate", 3: "High", 4: "Extreme"}
_RISK_LABELS = {
    "low": "Low",
    "moderate": "Moderate",
    "high": "High",
    "extreme": "Extreme",
    "unknown": "Unknown",
}
_YES_STRINGS = {"1", "true", "t", "yes", "y"}
_NO_STRINGS = {"0", "false", "f", "no", "n"}


def get_property(
    properties: Mapping[str, Any] | None,
    path: str,
    default: Any = None,
) -> Any:
    """Return a property from flattened, nested, or mixed dotted-key data.

    At each nesting level an exact match for the unconsumed dotted path takes
    precedence over traversal. This preserves the required flattened-first
    behavior while also supporting mixed structures such as
    ``{"metobject": {"risk.value": 3}}``.
    """

    if not isinstance(properties, Mapping) or not isinstance(path, str) or not path:
        return default

    current: Any = properties
    parts = path.split(".")
    for index, part in enumerate(parts):
        if not isinstance(current, Mapping):
            return default

        remainder = ".".join(parts[index:])
        flattened = current.get(remainder, _MISSING)
        if flattened is not _MISSING:
            return flattened

        nested = current.get(part, _MISSING)
        if nested is _MISSING:
            return default
        current = nested

    return current


def get_feature_property(
    feature: Mapping[str, Any] | None,
    path: str,
    default: Any = None,
) -> Any:
    """Read ``path`` from a GeoJSON feature's properties."""

    if not isinstance(feature, Mapping):
        return default
    properties = feature.get("properties")
    return get_property(properties if isinstance(properties, Mapping) else None, path, default)


def normalize_risk(value: Any) -> str:
    """Normalize a risk number or label to one of :data:`RISK_LEVELS`."""

    if value is None or isinstance(value, bool):
        return "Unknown"

    if isinstance(value, str):
        candidate = value.strip()
        if not candidate:
            return "Unknown"
        label = _RISK_LABELS.get(candidate.casefold())
        if label is not None:
            return label
        try:
            numeric = float(candidate)
        except ValueError:
            return "Unknown"
    elif isinstance(value, (int, float, Decimal)):
        try:
            numeric = float(value)
        except (TypeError, ValueError, OverflowError):
            return "Unknown"
    else:
        return "Unknown"

    if not math.isfinite(numeric) or not numeric.is_integer():
        return "Unknown"
    return _RISK_NUMBERS.get(int(numeric), "Unknown")


def risk_sort_key(value: Any) -> int:
    """Return the stable display order for a risk value."""

    return RISK_ORDER[normalize_risk(value)]


def risk_colour(value: Any) -> str:
    """Return a safe map colour for any risk value."""

    return RISK_COLOURS[normalize_risk(value)]


def normalize_contributor(value: Any) -> str:
    """Normalize a contributing-factor value to ``Yes``, ``No``, or ``Unknown``."""

    if value is None:
        return "Unknown"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, str):
        candidate = value.strip().casefold()
        if candidate in _YES_STRINGS:
            return "Yes"
        if candidate in _NO_STRINGS:
            return "No"
        return "Unknown"
    if isinstance(value, (int, float, Decimal)):
        try:
            numeric = float(value)
        except (TypeError, ValueError, OverflowError):
            return "Unknown"
        if not math.isfinite(numeric):
            return "Unknown"
        if numeric == 1:
            return "Yes"
        if numeric == 0:
            return "No"
    return "Unknown"


def parse_utc_datetime(value: Any) -> datetime | None:
    """Parse a datetime-like value and return an aware UTC datetime.

    Naive datetimes and ISO strings without an offset are interpreted as UTC.
    Invalid, missing, numeric, and non-finite values return ``None``.
    """

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        candidate = value.strip()
        if not candidate:
            return None
        if candidate.endswith(("Z", "z")):
            candidate = f"{candidate[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            return None
    else:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        return parsed.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        return None


def format_utc_datetime(value: Any) -> str:
    """Format a parseable datetime as ISO 8601 UTC, or return an empty string."""

    parsed = parse_utc_datetime(value)
    if parsed is None:
        return ""
    return parsed.isoformat().replace("+00:00", "Z")


def display_value(value: Any, *, missing: str = "") -> str:
    """Convert a value to safe, deterministic display text.

    Mapping and sequence values are rendered as JSON rather than Python
    representations. Non-finite numbers are treated as missing.
    """

    if value is None:
        return missing
    if isinstance(value, datetime):
        return format_utc_datetime(value) or missing
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and not math.isfinite(value):
        return missing
    if isinstance(value, Decimal):
        if not value.is_finite():
            return missing
        return str(value)
    if isinstance(value, (Mapping, list, tuple, set, frozenset)):
        return json.dumps(
            json_safe(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(", ", ": "),
            allow_nan=False,
        )
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def html_value(value: Any, *, missing: str = "") -> str:
    """Return escaped display text suitable for tooltips and popup HTML."""

    return escape(display_value(value, missing=missing), quote=True)


def json_safe(value: Any) -> Any:
    """Recursively convert arbitrary values to strict JSON-compatible values."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Decimal):
        if not value.is_finite():
            return None
        if value == value.to_integral_value():
            return int(value)
        return float(value)
    if isinstance(value, datetime):
        return format_utc_datetime(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return [json_safe(item) for item in sorted(value, key=str)]
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, Path):
        return str(value)
    return str(value)


def feature_source(feature: Mapping[str, Any]) -> str:
    """Return a clear table/map source label for a feature."""

    properties = feature.get("properties")
    props = properties if isinstance(properties, Mapping) else {}
    explicit = get_property(props, "source", _MISSING)
    if explicit is _MISSING:
        explicit = get_property(props, "data_source", _MISSING)
    if explicit is not _MISSING and display_value(explicit):
        return display_value(explicit)

    synthetic = get_property(props, "synthetic", False)
    if synthetic is True or str(synthetic).strip().casefold() in _YES_STRINGS:
        return "Synthetic test data"
    return "ECCC GeoMet"


def _datetime_table_value(value: Any) -> str:
    parsed = format_utc_datetime(value)
    return parsed if parsed else display_value(value)


def feature_record(feature: Mapping[str, Any]) -> dict[str, str]:
    """Build one stable, display-safe table record from a GeoJSON feature."""

    properties = feature.get("properties")
    props = properties if isinstance(properties, Mapping) else {}
    feature_id = feature.get("id")
    if feature_id in (None, ""):
        feature_id = get_property(props, "id")
    return {
        "feature_id": display_value(feature_id),
        "risk": normalize_risk(get_property(props, RISK_PROPERTY)),
        "impact": display_value(get_property(props, IMPACT_PROPERTY)),
        "likelihood": display_value(get_property(props, LIKELIHOOD_PROPERTY)),
        "tide": normalize_contributor(get_property(props, TIDE_PROPERTY)),
        "storm_surge": normalize_contributor(
            get_property(props, STORM_SURGE_PROPERTY)
        ),
        "waves": normalize_contributor(get_property(props, WAVES_PROPERTY)),
        "validity_datetime": _datetime_table_value(
            get_property(props, VALIDITY_PROPERTY)
        ),
        "publication_datetime": _datetime_table_value(
            get_property(props, PUBLICATION_PROPERTY)
        ),
        "expiration_datetime": _datetime_table_value(
            get_property(props, EXPIRATION_PROPERTY)
        ),
        "amendment": display_value(get_property(props, "amendment")),
        "domain": display_value(get_property(props, "domain")),
        "status": display_value(get_property(props, "status")),
        "file_id": display_value(get_property(props, "file_id")),
        "source": feature_source(feature),
    }


def feature_collection_to_dataframe(
    feature_collection: Mapping[str, Any] | None,
) -> pd.DataFrame:
    """Convert a FeatureCollection to a DataFrame with stable columns."""

    features: Any = (
        feature_collection.get("features")
        if isinstance(feature_collection, Mapping)
        else None
    )
    records = [
        feature_record(feature)
        for feature in features or []
        if isinstance(feature, Mapping)
    ]
    return pd.DataFrame.from_records(records, columns=TABLE_COLUMNS)


def safe_feature_collection(
    feature_collection: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return a strict-JSON-safe FeatureCollection without mutating its input."""

    raw_features: Any = (
        feature_collection.get("features")
        if isinstance(feature_collection, Mapping)
        else None
    )
    if not isinstance(raw_features, Sequence) or isinstance(
        raw_features, (str, bytes, bytearray)
    ):
        raw_features = []
    features = [json_safe(feature) for feature in raw_features if isinstance(feature, Mapping)]
    return {"type": "FeatureCollection", "features": features}


def serialize_feature_collection(
    feature_collection: Mapping[str, Any] | None,
) -> str:
    """Serialize a FeatureCollection as standards-compliant UTF-8 GeoJSON text."""

    return json.dumps(
        safe_feature_collection(feature_collection),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )


def feature_collection_bytes(
    feature_collection: Mapping[str, Any] | None,
) -> bytes:
    """Serialize a FeatureCollection as UTF-8 bytes for Streamlit download."""

    return serialize_feature_collection(feature_collection).encode("utf-8")


def export_filename(now: datetime | None = None) -> str:
    """Return a UTC timestamped GeoJSON download filename."""

    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    stamp = instant.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"eccc_coastal_flooding_{stamp}.geojson"


# Readable aliases for callers that prefer UI/export-oriented names.
property_value = get_property
escape_html_value = html_value
features_to_dataframe = feature_collection_to_dataframe
geojson_bytes = feature_collection_bytes
