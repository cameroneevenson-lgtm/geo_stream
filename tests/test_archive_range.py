"""Tests for inclusive ECCC archive range aggregation."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import date, datetime, timezone
from typing import Any

import pytest

from coastal_flood_explorer.archive import (
    ArchiveDocument,
    ArchiveFetchResult,
    ArchiveProduct,
    ECCCDatamartArchiveClient,
    ECCCArchiveDirectoryError,
    ECCCArchiveRequestError,
)
from coastal_flood_explorer.archive_range import (
    ArchiveRangeValidationError,
    combine_archive_range,
    fetch_archive_range,
    inclusive_archive_dates,
    raw_range_bundle_bytes,
)


def _product(issue_date: str, *, suffix: str = "a") -> ArchiveProduct:
    issue_time = datetime.strptime(
        f"{issue_date}T1200Z",
        "%Y%m%dT%H%MZ",
    ).replace(tzinfo=timezone.utc)
    filename = (
        f"{issue_date}T1200Z_MSC_CoastalFloodingRiskIndex_"
        f"ASPC_{suffix}_PT012H00M_v1.json"
    )
    return ArchiveProduct(
        filename=filename,
        url=f"https://dd.weather.gc.ca/{issue_date}/{filename}",
        logical_name=filename.removesuffix("_v1.json"),
        version=1,
        issue_time=issue_time,
        valid_time=issue_time,
        office="ASPC",
        coverage=suffix,
        lead_hours=12,
        lead_minutes=0,
    )


def _result(
    issue_date: str,
    identifiers: list[str],
    *,
    score: float = 1.0,
    duplicate_product: bool = False,
) -> ArchiveFetchResult:
    product = _product(issue_date)
    features = [
        {
            "type": "Feature",
            "id": identifier,
            "geometry": None,
            "properties": {"identifier": identifier, "score": score},
        }
        for identifier in identifiers
    ]
    payload: dict[str, Any] = {
        "type": "FeatureCollection",
        "features": deepcopy(features),
        "vendor": {"retained": True},
    }
    products = (product, product) if duplicate_product else (product,)
    documents = (
        (ArchiveDocument(product, payload),) * 2
        if duplicate_product
        else (ArchiveDocument(product, payload),)
    )
    return ArchiveFetchResult(
        collection={"type": "FeatureCollection", "features": features},
        products=products,
        documents=documents,
    )


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (date(2026, 7, 1), date(2026, 7, 1), ("20260701",)),
        (
            "20260701",
            "20260703",
            ("20260701", "20260702", "20260703"),
        ),
    ],
)
def test_inclusive_archive_dates_are_chronological_and_inclusive(
    start: str | date,
    end: str | date,
    expected: tuple[str, ...],
) -> None:
    assert inclusive_archive_dates(start, end) == expected


def test_inclusive_archive_dates_accept_exactly_thirty_days() -> None:
    values = inclusive_archive_dates("20260701", "20260730")

    assert len(values) == 30
    assert values[0] == "20260701"
    assert values[-1] == "20260730"


@pytest.mark.parametrize(
    ("start", "end", "fragment"),
    [
        ("20260702", "20260701", "start date"),
        ("20260701", "20260731", "at most 30"),
        ("20260230", "20260301", "valid calendar"),
    ],
)
def test_inclusive_archive_dates_reject_invalid_bounds(
    start: str,
    end: str,
    fragment: str,
) -> None:
    with pytest.raises(ValueError, match=fragment):
        inclusive_archive_dates(start, end)


def test_inclusive_archive_dates_reject_datetime_bounds() -> None:
    with pytest.raises(ValueError, match="without a time"):
        inclusive_archive_dates(
            datetime(2026, 7, 1, tzinfo=timezone.utc),
            date(2026, 7, 2),
        )


def test_combiner_preserves_successes_empty_day_and_failure_diagnostics() -> None:
    first = _result("20260701", ["one"])
    empty = _result("20260702", [])
    failure = ECCCArchiveDirectoryError("No official products were listed.")

    combined = combine_archive_range(
        "20260701",
        "20260703",
        successes=[("20260701", first), ("20260702", empty)],
        failures=[("20260703", failure)],
    )

    assert combined.start_date == "20260701"
    assert combined.end_date == "20260703"
    assert combined.requested_date_count == 3
    assert combined.successful_date_count == 2
    assert combined.failed_date_count == 1
    assert combined.feature_count == 1
    assert combined.collection["features"] == first.collection["features"]
    assert [outcome.issue_date for outcome in combined.outcomes] == [
        "20260701",
        "20260702",
        "20260703",
    ]
    assert combined.outcomes[1].succeeded
    assert combined.outcomes[1].feature_count == 0
    assert combined.outcomes[2].error_type == "ECCCArchiveDirectoryError"
    assert (
        combined.outcomes[2].error_message
        == "No official products were listed."
    )


def test_combiner_does_not_mutate_or_share_input_payloads() -> None:
    first = _result("20260701", ["same"])
    second = _result("20260702", ["same"])
    first_before = deepcopy(first)
    second_before = deepcopy(second)

    combined = combine_archive_range(
        "20260701",
        "20260702",
        successes=[("20260701", first), ("20260702", second)],
    )

    assert [item["id"] for item in combined.collection["features"]] == [
        "same",
        "same",
    ]
    combined.collection["features"][0]["properties"]["score"] = 99
    combined.outcomes[0].result.collection["features"][0]["id"] = "changed"
    combined.outcomes[0].result.documents[0].payload["vendor"] = {}
    assert first == first_before
    assert second == second_before


def test_combiner_deduplicates_aggregate_product_and_document_views() -> None:
    source = _result(
        "20260701",
        ["one"],
        duplicate_product=True,
    )

    combined = combine_archive_range(
        "20260701",
        "20260701",
        successes=[("20260701", source)],
    )

    assert len(combined.products) == 1
    assert len(combined.documents) == 1
    assert combined.outcomes[0].product_count == 2


@pytest.mark.parametrize(
    ("successes", "failures", "fragment"),
    [
        ([], [], "missing 20260701"),
        (
            [("20260701", _result("20260701", []))],
            [("20260701", "duplicate")],
            "more than once",
        ),
        (
            [("20260702", _result("20260702", []))],
            [],
            "outside the requested range",
        ),
        (
            [("20260701", object())],
            [],
            "ArchiveFetchResult",
        ),
        (
            [],
            [("20260701", "")],
            "must not be empty",
        ),
    ],
)
def test_combiner_rejects_incomplete_or_inconsistent_outcomes(
    successes: list[tuple[str, object]],
    failures: list[tuple[str, object]],
    fragment: str,
) -> None:
    with pytest.raises(ArchiveRangeValidationError, match=fragment):
        combine_archive_range(
            "20260701",
            "20260701",
            successes=successes,  # type: ignore[arg-type]
            failures=failures,  # type: ignore[arg-type]
        )


def test_combiner_rejects_result_products_from_a_different_date() -> None:
    with pytest.raises(
        ArchiveRangeValidationError,
        match="did not match its issue date",
    ):
        combine_archive_range(
            "20260701",
            "20260701",
            successes=[("20260701", _result("20260702", []))],
        )


class _StubClient(ECCCDatamartArchiveClient):
    def __init__(
        self,
        outcomes: dict[str, ArchiveFetchResult | Exception],
    ) -> None:
        self.outcomes = outcomes
        self.calls: list[str] = []

    def fetch_date(self, archive_date: str | date) -> ArchiveFetchResult:
        issue_date = (
            archive_date
            if isinstance(archive_date, str)
            else archive_date.strftime("%Y%m%d")
        )
        self.calls.append(issue_date)
        outcome = self.outcomes[issue_date]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_fetch_archive_range_retains_partial_errors_and_attempts_all_days() -> None:
    client = _StubClient(
        {
            "20260701": _result("20260701", ["one"]),
            "20260702": ECCCArchiveDirectoryError("missing"),
            "20260703": _result("20260703", ["three"]),
        }
    )

    result = fetch_archive_range(client, "20260701", "20260703")

    assert client.calls == ["20260701", "20260702", "20260703"]
    assert result.successful_date_count == 2
    assert result.failed_date_count == 1
    assert [feature["id"] for feature in result.collection["features"]] == [
        "one",
        "three",
    ]


def test_fetch_archive_range_stops_after_systemic_service_failure() -> None:
    client = _StubClient(
        {
            "20260701": ECCCArchiveRequestError(
                "service unavailable",
                status_code=503,
                systemic=True,
            ),
        }
    )

    result = fetch_archive_range(client, "20260701", "20260703")

    assert client.calls == ["20260701"]
    assert result.successful_date_count == 0
    assert result.failed_date_count == 3
    assert result.outcomes[0].error_type == "ECCCArchiveRequestError"
    assert all(
        outcome.error_message is not None
        and "Not attempted" in outcome.error_message
        for outcome in result.outcomes[1:]
    )


def test_fetch_archive_range_stops_at_cumulative_feature_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "coastal_flood_explorer.archive_range.MAX_TOTAL_FEATURES",
        1,
    )
    client = _StubClient(
        {
            "20260701": _result("20260701", ["one"]),
        }
    )

    result = fetch_archive_range(client, "20260701", "20260703")

    assert client.calls == ["20260701"]
    assert result.successful_date_count == 1
    assert result.failed_date_count == 2
    assert [feature["id"] for feature in result.collection["features"]] == [
        "one"
    ]
    assert all(
        outcome.error_message is not None
        and "Not attempted" in outcome.error_message
        for outcome in result.outcomes[1:]
    )


def test_fetch_archive_range_can_require_transactional_success() -> None:
    failure = ECCCArchiveDirectoryError("missing")
    client = _StubClient(
        {
            "20260701": _result("20260701", ["one"]),
            "20260702": failure,
            "20260703": _result("20260703", ["never requested"]),
        }
    )

    with pytest.raises(ECCCArchiveDirectoryError, match="missing"):
        fetch_archive_range(
            client,
            "20260701",
            "20260703",
            allow_partial=False,
        )

    assert client.calls == ["20260701", "20260702"]


def test_raw_range_bundle_retains_each_date_files_and_strict_json() -> None:
    success = _result("20260701", ["côte"], score=float("nan"))
    original = deepcopy(success)
    combined = combine_archive_range(
        "20260701",
        "20260702",
        successes=[("20260701", success)],
        failures=[("20260702", "Archive partition is not available.")],
    )

    encoded = raw_range_bundle_bytes(combined)
    decoded = json.loads(encoded.decode("utf-8"))

    assert decoded["date_range"] == {
        "start": "20260701",
        "end": "20260702",
        "inclusive_day_count": 2,
    }
    assert decoded["summary"] == {
        "successful_date_count": 1,
        "not_loaded_date_count": 1,
        "feature_count": 1,
        "product_count": 1,
    }
    assert decoded["dates"][0]["status"] == "success"
    assert decoded["dates"][0]["files"][0]["payload"]["features"][0][
        "properties"
    ]["score"] is None
    assert decoded["dates"][1] == {
        "issue_date": "20260702",
        "status": "not_loaded",
        "error": {
            "type": "ArchiveDateFailure",
            "message": "Archive partition is not available.",
        },
        "files": [],
    }
    assert success == original
    assert b"NaN" not in encoded


def test_all_failed_range_is_valid_empty_result_and_download() -> None:
    combined = combine_archive_range(
        "20260701",
        "20260702",
        failures=[
            ("20260701", "missing"),
            ("20260702", "missing"),
        ],
    )

    assert combined.collection == {
        "type": "FeatureCollection",
        "features": [],
    }
    assert combined.successful_date_count == 0
    assert combined.failed_date_count == 2
    assert json.loads(raw_range_bundle_bytes(combined))["summary"][
        "successful_date_count"
    ] == 0
