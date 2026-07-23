"""Unit tests for the ECCC Datamart archive client."""

from __future__ import annotations

from collections import deque
from copy import deepcopy
from datetime import date, datetime
import json
from typing import Any

import pytest
import requests

from coastal_flood_explorer.api import REQUEST_TIMEOUT, USER_AGENT
from coastal_flood_explorer.archive import (
    ARCHIVE_BASE_URL,
    ECCC_ARCHIVE_ROOT,
    MAX_ARCHIVE_FILES,
    MAX_TOTAL_FEATURES,
    ArchiveError,
    ArchiveFetchResult,
    ArchiveDateValidationError,
    ECCCArchiveClient,
    ECCCDatamartArchiveClient,
    ECCCArchiveConfigurationError,
    ECCCArchiveDirectoryError,
    ECCCArchiveRequestError,
    ECCCArchiveResponseError,
    build_archive_directory_url,
    raw_bundle_bytes,
    validate_archive_date,
)

ARCHIVE_DATE = "20260722"
DIRECTORY_URL = (
    f"{ECCC_ARCHIVE_ROOT}/{ARCHIVE_DATE}/WXO-DD/"
    "coastal-flooding/risk-index/"
)


def product_filename(
    *,
    stamp: str = "20260722T2200Z",
    office: str = "ASPC",
    region: str = "MARITIMES",
    lead: str = "PT014H00M",
    version: int = 1,
) -> str:
    return (
        f"{stamp}_MSC_CoastalFloodingRiskIndex_{office}_{region}_"
        f"{lead}_v{version}.json"
    )


def feature(identifier: str) -> dict[str, Any]:
    return {
        "type": "Feature",
        "id": identifier,
        "geometry": None,
        "properties": {"identifier": identifier},
    }


def directory_html(*hrefs: str) -> str:
    links = "".join(f'<a href="{href}">file</a>' for href in hrefs)
    return f"<!doctype html><html><body>{links}</body></html>"


class FakeResponse:
    def __init__(
        self,
        payload: Any = None,
        *,
        text: str = "",
        status_code: int = 200,
        content_type: str = "application/geo+json",
        json_error: ValueError | None = None,
    ) -> None:
        self.payload = payload
        self.text = text
        self.status_code = status_code
        self.headers = {"Content-Type": content_type}
        self.json_error = json_error

    def json(self) -> Any:
        if self.json_error is not None:
            raise self.json_error
        return self.payload


class FakeSession:
    def __init__(self, *responses: FakeResponse | BaseException) -> None:
        self.responses = deque(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.headers: dict[str, str] = {}
        self.mounts: dict[str, Any] = {}

    def mount(self, prefix: str, adapter: Any) -> None:
        self.mounts[prefix] = adapter

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError("Unexpected mocked HTTP request")
        result = self.responses.popleft()
        if isinstance(result, BaseException):
            raise result
        return result


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("20260722", "20260722"),
        (date(2026, 7, 22), "20260722"),
    ],
)
def test_validate_archive_date_accepts_real_dates(
    value: str | date,
    expected: str,
) -> None:
    assert validate_archive_date(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "2026-07-22",
        "2026072",
        "202607220",
        "20260230",
        datetime(2026, 7, 22, 12),
        None,
    ],
)
def test_validate_archive_date_rejects_invalid_or_ambiguous_dates(
    value: Any,
) -> None:
    with pytest.raises(ArchiveDateValidationError):
        validate_archive_date(value)


def test_build_archive_directory_url_uses_wxo_dd_partition() -> None:
    assert ARCHIVE_BASE_URL == ECCC_ARCHIVE_ROOT
    assert build_archive_directory_url(ARCHIVE_DATE) == DIRECTORY_URL
    assert issubclass(ECCCArchiveDirectoryError, ArchiveError)


def test_injected_session_gets_project_user_agent_and_retry_adapters() -> None:
    session = FakeSession()

    ECCCArchiveClient(session=session)

    assert session.headers["User-Agent"] == USER_AGENT
    assert set(session.mounts) == {"http://", "https://"}


def test_discover_selects_latest_amendment_and_sorts_products() -> None:
    logical_v1 = product_filename(version=1)
    logical_v3 = product_filename(version=3)
    logical_v2 = product_filename(version=2)
    second = product_filename(
        stamp="20260722T0200Z",
        office="PSPC",
        region="BC",
        lead="PT010H00M",
    )
    session = FakeSession(
        FakeResponse(
            text=directory_html(
                logical_v1,
                logical_v3,
                logical_v2,
                logical_v3,
                second,
            ),
            content_type="text/html; charset=UTF-8",
        )
    )

    products = ECCCArchiveClient(session=session).discover(ARCHIVE_DATE)

    assert [item.filename for item in products] == [second, logical_v3]
    assert products[1].version == 3
    assert products[1].logical_name == logical_v3.removesuffix("_v3.json")
    assert session.calls == [
        (
            DIRECTORY_URL,
            {"timeout": REQUEST_TIMEOUT, "allow_redirects": False},
        )
    ]


def test_discover_accepts_absolute_same_directory_official_link() -> None:
    filename = product_filename()
    session = FakeSession(
        FakeResponse(
            text=directory_html(f"{DIRECTORY_URL}{filename}"),
            content_type="application/xhtml+xml",
        )
    )

    products = ECCCArchiveClient(session=session).discover(ARCHIVE_DATE)

    assert len(products) == 1
    assert products[0].url == f"{DIRECTORY_URL}{filename}"


def test_list_products_accepts_lowercase_codes_and_hour_only_lead() -> None:
    filename = product_filename(
        office="aspc",
        region="maritimes",
        lead="PT014H",
        version=2,
    )
    session = FakeSession(
        FakeResponse(
            text=directory_html(filename),
            content_type="text/html",
        )
    )

    products = ECCCDatamartArchiveClient(session=session).list_products(
        ARCHIVE_DATE
    )

    assert len(products) == 1
    product = products[0]
    assert product.office == "aspc"
    assert product.coverage == "maritimes"
    assert product.lead_hours == 14
    assert product.lead_minutes == 0
    assert product.issue_time.isoformat() == "2026-07-22T22:00:00+00:00"
    assert product.valid_time.isoformat() == "2026-07-23T12:00:00+00:00"
    assert product.label == "2026-07-22 22:00Z · aspc/maritimes · +14h"
    assert product.metadata()["valid_time"] == "2026-07-23T12:00:00Z"


def test_list_products_deduplicates_equivalent_lead_spelling_and_code_case() -> None:
    first = product_filename(
        office="aspc",
        region="maritimes",
        lead="PT014H",
        version=1,
    )
    amendment = product_filename(
        office="ASPC",
        region="MARITIMES",
        lead="PT014H00M",
        version=2,
    )
    session = FakeSession(
        FakeResponse(
            text=directory_html(first, amendment),
            content_type="text/html",
        )
    )

    products = ECCCDatamartArchiveClient(session=session).list_products(
        ARCHIVE_DATE
    )

    assert [product.filename for product in products] == [amendment]


@pytest.mark.parametrize(
    "unsafe_href",
    [
        "https://example.com/20260722/product.json",
        "http://dd.weather.gc.ca/20260722/product.json",
        "https://user:password@dd.weather.gc.ca/product.json",
        "../20260722T2200Z_MSC_CoastalFloodingRiskIndex_ASPC_MARITIMES_"
        "PT014H00M_v1.json",
        "nested/20260722T2200Z_MSC_CoastalFloodingRiskIndex_ASPC_MARITIMES_"
        "PT014H00M_v1.json",
        "20260722T2200Z_MSC_CoastalFloodingRiskIndex_ASPC_MARITIMES_"
        "PT014H00M_v1.json?download=1",
        "20260722T2200Z_MSC_CoastalFloodingRiskIndex_ASPC_MARITIMES_"
        "PT014H00M_v1%2ejson",
        "20260721T2200Z_MSC_CoastalFloodingRiskIndex_ASPC_MARITIMES_"
        "PT014H00M_v1.json",
        "20260722T2561Z_MSC_CoastalFloodingRiskIndex_ASPC_MARITIMES_"
        "PT014H00M_v1.json",
        "20260722T2200Z_MSC_CoastalFloodingRiskIndex_ASPC_MARITIMES_"
        "PT014H60M_v1.json",
        "notes.json",
    ],
)
def test_discover_ignores_unsafe_or_nonofficial_links(
    unsafe_href: str,
) -> None:
    session = FakeSession(
        FakeResponse(
            text=directory_html(unsafe_href),
            content_type="text/html",
        )
    )

    assert ECCCArchiveClient(session=session).discover(ARCHIVE_DATE) == ()


def test_fetch_merges_selected_files_into_fresh_feature_collection() -> None:
    first_name = product_filename(
        stamp="20260722T0200Z",
        office="PSPC",
        region="BC",
        lead="PT010H00M",
    )
    second_v1 = product_filename(version=1)
    second_v2 = product_filename(version=2)
    first_feature = feature("first")
    second_feature = feature("second")
    session = FakeSession(
        FakeResponse(
            text=directory_html(first_name, second_v1, second_v2),
            content_type="text/html",
        ),
        FakeResponse(
            {
                "type": "FeatureCollection",
                "features": [first_feature],
                "links": [{"rel": "self", "href": "ignored"}],
            },
            content_type="application/json",
        ),
        FakeResponse(
            {
                "type": "FeatureCollection",
                "features": [second_feature],
                "numberReturned": 1,
            }
        ),
    )

    result = ECCCArchiveClient(session=session).fetch(ARCHIVE_DATE)

    assert result == {
        "type": "FeatureCollection",
        "features": [first_feature, second_feature],
    }
    assert set(result) == {"type", "features"}
    assert [call[0] for call in session.calls] == [
        DIRECTORY_URL,
        f"{DIRECTORY_URL}{first_name}",
        f"{DIRECTORY_URL}{second_v2}",
    ]
    assert all(
        call[1] == {
            "timeout": REQUEST_TIMEOUT,
            "allow_redirects": False,
        }
        for call in session.calls
    )


def test_fetch_date_retains_raw_documents_without_shared_feature_mutation() -> None:
    filename = product_filename()
    payload = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "id": "source-id",
                "geometry": {
                    "type": "Point",
                    "coordinates": [-63.57, 44.65],
                },
                "properties": {"risk": "Élevé", "nested": {"kept": True}},
            }
        ],
        "numberReturned": 1,
        "links": [{"rel": "self", "href": "official-source"}],
    }
    original = deepcopy(payload)
    session = FakeSession(
        FakeResponse(
            text=directory_html(filename),
            content_type="text/html",
        ),
        FakeResponse(payload),
    )

    result = ECCCDatamartArchiveClient(session=session).fetch_date(
        ARCHIVE_DATE
    )

    assert isinstance(result, ArchiveFetchResult)
    assert result.products == (result.documents[0].product,)
    assert result.documents[0].payload == original
    assert payload == original
    result.collection["features"][0]["properties"]["nested"]["kept"] = False
    assert result.documents[0].payload == original
    assert payload == original


def test_raw_bundle_bytes_is_strict_utf8_json_and_does_not_mutate_payload() -> None:
    filename = product_filename()
    payload = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "id": "côte",
                "geometry": None,
                "properties": {"label": "Baie de Fundy", "score": float("nan")},
            }
        ],
        "vendorMetadata": {"unchanged": ["yes"]},
    }
    session = FakeSession(
        FakeResponse(
            text=directory_html(filename),
            content_type="text/html",
        ),
        FakeResponse(payload),
    )
    result = ECCCDatamartArchiveClient(session=session).fetch_date(
        ARCHIVE_DATE
    )
    before = deepcopy(result.documents[0].payload)

    encoded = raw_bundle_bytes(result, date(2026, 7, 22))
    decoded = json.loads(encoded.decode("utf-8"))

    assert decoded["issue_date"] == ARCHIVE_DATE
    assert "Environment and Climate Change Canada" in decoded["source"]
    assert decoded["files"] == [
        {
            "filename": filename,
            "url": f"{DIRECTORY_URL}{filename}",
            "payload": {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "id": "côte",
                        "geometry": None,
                        "properties": {
                            "label": "Baie de Fundy",
                            "score": None,
                        },
                    }
                ],
                "vendorMetadata": {"unchanged": ["yes"]},
            },
        }
    ]
    assert result.documents[0].payload == before


def test_fetch_preserves_feature_ids_properties_and_malformed_entries() -> None:
    filename = product_filename()
    entries: list[Any] = [feature("kept"), {}, "malformed", None]
    session = FakeSession(
        FakeResponse(
            text=directory_html(filename),
            content_type="text/html",
        ),
        FakeResponse(
            {"type": "FeatureCollection", "features": entries},
        ),
    )

    result = ECCCArchiveClient(session=session).fetch(ARCHIVE_DATE)

    assert result["features"] == entries
    assert result["features"][0]["id"] == "kept"
    assert result["features"][0]["properties"] == {"identifier": "kept"}


def test_fetch_reports_directory_without_official_products() -> None:
    session = FakeSession(
        FakeResponse(
            text=directory_html("README.txt"),
            content_type="text/html",
        )
    )

    with pytest.raises(ECCCArchiveDirectoryError, match="did not list"):
        ECCCArchiveClient(session=session).fetch(ARCHIVE_DATE)
    assert len(session.calls) == 1


@pytest.mark.parametrize("content_type", ["application/json", "text/plain", ""])
def test_discover_rejects_non_html_directory(content_type: str) -> None:
    session = FakeSession(
        FakeResponse(text="<html></html>", content_type=content_type)
    )
    with pytest.raises(ECCCArchiveDirectoryError, match="did not return HTML"):
        ECCCArchiveClient(session=session).discover(ARCHIVE_DATE)


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"type": "Feature", "features": []},
        {"type": "FeatureCollection"},
        {"type": "FeatureCollection", "features": None},
    ],
)
def test_fetch_rejects_malformed_feature_collection(payload: Any) -> None:
    filename = product_filename()
    session = FakeSession(
        FakeResponse(
            text=directory_html(filename),
            content_type="text/html",
        ),
        FakeResponse(payload),
    )
    with pytest.raises(ECCCArchiveResponseError):
        ECCCArchiveClient(session=session).fetch(ARCHIVE_DATE)


def test_fetch_rejects_invalid_product_json_without_leaking_details() -> None:
    filename = product_filename()
    session = FakeSession(
        FakeResponse(
            text=directory_html(filename),
            content_type="text/html",
        ),
        FakeResponse(
            json_error=ValueError("private decoder detail"),
        ),
    )
    with pytest.raises(
        ECCCArchiveResponseError,
        match="did not contain valid JSON",
    ) as exc:
        ECCCArchiveClient(session=session).fetch(ARCHIVE_DATE)
    assert "private decoder detail" not in str(exc.value)


def test_fetch_rejects_unsupported_product_content_type() -> None:
    filename = product_filename()
    session = FakeSession(
        FakeResponse(
            text=directory_html(filename),
            content_type="text/html",
        ),
        FakeResponse(
            {"type": "FeatureCollection", "features": []},
            content_type="text/html",
        ),
    )
    with pytest.raises(ECCCArchiveResponseError, match="content type"):
        ECCCArchiveClient(session=session).fetch(ARCHIVE_DATE)


@pytest.mark.parametrize(
    ("status_code", "message_fragment"),
    [
        (404, "retention window"),
        (429, "limiting"),
        (500, "temporarily unavailable"),
        (400, "rejected"),
        (302, "unexpected"),
    ],
)
def test_request_translates_http_errors(
    status_code: int,
    message_fragment: str,
) -> None:
    session = FakeSession(
        FakeResponse(status_code=status_code, content_type="text/plain")
    )
    with pytest.raises(ECCCArchiveRequestError, match=message_fragment):
        ECCCArchiveClient(session=session).discover(ARCHIVE_DATE)


@pytest.mark.parametrize(
    ("request_error", "message_fragment"),
    [
        (requests.Timeout("details"), "timed out"),
        (requests.ConnectionError("details"), "Could not connect"),
        (requests.RequestException("details"), "could not be completed"),
    ],
)
def test_request_translates_network_errors(
    request_error: requests.RequestException,
    message_fragment: str,
) -> None:
    session = FakeSession(request_error)
    with pytest.raises(ECCCArchiveRequestError, match=message_fragment) as exc:
        ECCCArchiveClient(session=session).discover(ARCHIVE_DATE)
    assert "details" not in str(exc.value)


def test_discover_stops_when_directory_has_too_many_logical_products() -> None:
    filenames = [
        product_filename(
            lead=f"PT{hour:03d}H00M",
        )
        for hour in range(2)
    ]
    session = FakeSession(
        FakeResponse(
            text=directory_html(*filenames),
            content_type="text/html",
        )
    )
    with pytest.raises(ECCCArchiveDirectoryError, match="more than 1"):
        ECCCArchiveClient(session=session, max_files=1).discover(ARCHIVE_DATE)


def test_fetch_stops_before_aggregating_too_many_features() -> None:
    filename = product_filename()
    session = FakeSession(
        FakeResponse(
            text=directory_html(filename),
            content_type="text/html",
        ),
        FakeResponse(
            {
                "type": "FeatureCollection",
                "features": [feature("one"), feature("two")],
            }
        ),
    )
    with pytest.raises(ECCCArchiveResponseError, match="more than 1"):
        ECCCArchiveClient(session=session, max_features=1).fetch(ARCHIVE_DATE)


@pytest.mark.parametrize(
    "archive_root",
    [
        "",
        "http://dd.weather.gc.ca",
        "https://user:password@dd.weather.gc.ca",
        "https://dd.weather.gc.ca/archive",
        "https://dd.weather.gc.ca?date=20260722",
        "not a URL",
    ],
)
def test_client_rejects_unsafe_archive_root(archive_root: str) -> None:
    with pytest.raises(ECCCArchiveConfigurationError, match="archive root"):
        ECCCArchiveClient(
            archive_root=archive_root,
            session=FakeSession(),
        )


@pytest.mark.parametrize(
    ("argument", "value", "message_fragment"),
    [
        ("max_files", 0, "Archive file limit"),
        ("max_files", MAX_ARCHIVE_FILES + 1, "Archive file limit"),
        ("max_files", True, "Archive file limit"),
        ("max_features", 0, "Feature limit"),
        ("max_features", MAX_TOTAL_FEATURES + 1, "Feature limit"),
        ("max_features", 1.5, "Feature limit"),
    ],
)
def test_client_rejects_invalid_limits(
    argument: str,
    value: Any,
    message_fragment: str,
) -> None:
    kwargs = {argument: value}
    with pytest.raises(ECCCArchiveConfigurationError, match=message_fragment):
        ECCCArchiveClient(session=FakeSession(), **kwargs)
