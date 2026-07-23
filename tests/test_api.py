"""Unit tests for the ECCC API client (all HTTP traffic is mocked)."""

from __future__ import annotations

from collections import deque
from typing import Any

import pytest
import requests

from coastal_flood_explorer.api import (
    ECCC_API_URL,
    MAX_PAGES,
    MAX_TOTAL_FEATURES,
    PAGE_LIMIT,
    REQUEST_TIMEOUT,
    RETRY_BACKOFF_FACTOR,
    RETRY_COUNT,
    RETRY_STATUS_CODES,
    USER_AGENT,
    BBoxValidationError,
    ECCCClient,
    ECCCConfigurationError,
    ECCCPaginationError,
    ECCCRequestError,
    ECCCResponseError,
    build_retry_session,
    validate_bbox,
)


def feature(identifier: str) -> dict[str, Any]:
    return {
        "type": "Feature",
        "id": identifier,
        "geometry": None,
        "properties": {},
    }


class FakeResponse:
    def __init__(
        self,
        payload: Any,
        *,
        status_code: int = 200,
        content_type: str = "application/geo+json",
        json_error: ValueError | None = None,
    ) -> None:
        self.payload = payload
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
    ("bbox", "message_fragment"),
    [
        ((1, 2, 3), "four numeric"),
        ((1, 2, 3, 4, 5), "four numeric"),
        (("west", 2, 3, 4), "non-numeric"),
        ((True, 2, 3, 4), "non-numeric"),
        ((float("nan"), 2, 3, 4), "non-finite"),
        ((float("inf"), 2, 3, 4), "non-finite"),
        ((-181, 2, 3, 4), "longitudes"),
        ((1, -91, 3, 4), "latitudes"),
        ((3, 2, 1, 4), "ordered"),
        ((1, 4, 3, 2), "ordered"),
        ((1, 2, 1, 4), "non-zero"),
    ],
)
def test_validate_bbox_rejects_invalid_values(
    bbox: Any,
    message_fragment: str,
) -> None:
    with pytest.raises(BBoxValidationError, match=message_fragment):
        validate_bbox(bbox)


def test_validate_bbox_returns_floats_in_crs84_order() -> None:
    assert validate_bbox((-66, 43, -59, 48)) == (
        -66.0,
        43.0,
        -59.0,
        48.0,
    )


def test_default_session_has_user_agent_and_bounded_get_retries() -> None:
    session = build_retry_session()
    retry = session.get_adapter("https://").max_retries

    assert session.headers["User-Agent"] == USER_AGENT
    assert retry.total == RETRY_COUNT
    assert retry.connect == RETRY_COUNT
    assert retry.read == RETRY_COUNT
    assert retry.status == RETRY_COUNT
    assert retry.backoff_factor == RETRY_BACKOFF_FACTOR
    assert retry.allowed_methods == frozenset({"GET"})
    assert set(retry.status_forcelist) == set(RETRY_STATUS_CODES)
    assert retry.respect_retry_after_header is True


def test_fetch_sends_expected_initial_query_and_timeout() -> None:
    session = FakeSession(
        FakeResponse({"type": "FeatureCollection", "features": []})
    )

    result = ECCCClient(session=session).fetch((-66, 43, -59, 48))

    assert result == {"type": "FeatureCollection", "features": []}
    assert session.headers["User-Agent"] == USER_AGENT
    assert len(session.calls) == 1
    url, kwargs = session.calls[0]
    assert url == ECCC_API_URL
    assert kwargs == {
        "params": {
            "f": "json",
            "bbox": "-66,43,-59,48",
            "limit": PAGE_LIMIT,
            "lang": "en",
        },
        "timeout": REQUEST_TIMEOUT,
        "allow_redirects": False,
    }


def test_fetch_supports_french_language() -> None:
    session = FakeSession(
        FakeResponse(
            {"type": "FeatureCollection", "features": []},
            content_type="application/json; charset=UTF-8",
        )
    )

    ECCCClient(session=session).fetch((-66, 43, -59, 48), "FR")

    assert session.calls[0][1]["params"]["lang"] == "fr"


@pytest.mark.parametrize("language", ["", "de", 7, None])
def test_fetch_rejects_unsupported_language(language: Any) -> None:
    client = ECCCClient(session=FakeSession())
    with pytest.raises(ECCCConfigurationError, match="language"):
        client.fetch((-66, 43, -59, 48), language)


def test_fetch_aggregates_absolute_and_relative_pagination() -> None:
    first = {
        "type": "FeatureCollection",
        "features": [feature("one")],
        "links": [
            {
                "rel": "next",
                "href": (
                    f"{ECCC_API_URL}?f=json&bbox=-66%2C43%2C-59%2C48"
                    "&limit=10000&lang=en&offset=1"
                ),
            }
        ],
    }
    second = {
        "type": "FeatureCollection",
        "features": [feature("two")],
        "links": [{"rel": ["collection", "next"], "href": "?offset=2"}],
    }
    third = {
        "type": "FeatureCollection",
        "features": [feature("three")],
        "links": [],
    }
    session = FakeSession(
        FakeResponse(first),
        FakeResponse(second, content_type="application/json"),
        FakeResponse(third),
    )

    result = ECCCClient(session=session).fetch((-66, 43, -59, 48))

    assert [item["id"] for item in result["features"]] == [
        "one",
        "two",
        "three",
    ]
    assert set(result) == {"type", "features"}
    assert session.calls[1] == (
        first["links"][0]["href"],
        {"timeout": REQUEST_TIMEOUT, "allow_redirects": False},
    )
    assert session.calls[2] == (
        f"{ECCC_API_URL}?offset=2",
        {"timeout": REQUEST_TIMEOUT, "allow_redirects": False},
    )


@pytest.mark.parametrize(
    "content_type",
    ["application/json", "application/geo+json; charset=utf-8"],
)
def test_fetch_accepts_supported_json_media_types(content_type: str) -> None:
    session = FakeSession(
        FakeResponse(
            {"type": "FeatureCollection", "features": []},
            content_type=content_type,
        )
    )
    assert ECCCClient(session=session).fetch((-66, 43, -59, 48))[
        "features"
    ] == []


@pytest.mark.parametrize(
    "content_type",
    ["text/html", "application/problem+json", ""],
)
def test_fetch_rejects_unsupported_content_type(content_type: str) -> None:
    session = FakeSession(
        FakeResponse(
            {"type": "FeatureCollection", "features": []},
            content_type=content_type,
        )
    )
    with pytest.raises(ECCCResponseError, match="content type"):
        ECCCClient(session=session).fetch((-66, 43, -59, 48))


def test_fetch_wraps_invalid_json_in_user_safe_error() -> None:
    session = FakeSession(
        FakeResponse(
            None,
            json_error=ValueError("decoder detail that should stay internal"),
        )
    )
    with pytest.raises(ECCCResponseError, match="did not contain valid JSON") as exc:
        ECCCClient(session=session).fetch((-66, 43, -59, 48))
    assert "decoder detail" not in str(exc.value)


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"type": "NotAFeatureCollection", "features": []},
        {"type": "FeatureCollection"},
        {"type": "FeatureCollection", "features": None},
    ],
)
def test_fetch_rejects_malformed_feature_collections(payload: Any) -> None:
    session = FakeSession(FakeResponse(payload))
    with pytest.raises(ECCCResponseError):
        ECCCClient(session=session).fetch((-66, 43, -59, 48))


def test_fetch_preserves_malformed_feature_entries_for_clipping_diagnostics() -> None:
    entries: list[Any] = [feature("valid"), {}, "bad feature", None]
    session = FakeSession(
        FakeResponse({"type": "FeatureCollection", "features": entries})
    )

    result = ECCCClient(session=session).fetch((-66, 43, -59, 48))

    assert result["features"] == entries


def test_fetch_detects_pagination_loop_before_repeating_request() -> None:
    first_url = (
        f"{ECCC_API_URL}?bbox=-66%2C43%2C-59%2C48&f=json"
        "&lang=en&limit=10000"
    )
    session = FakeSession(
        FakeResponse(
            {
                "type": "FeatureCollection",
                "features": [feature("one")],
                "links": [{"rel": "next", "href": first_url}],
            }
        )
    )

    with pytest.raises(ECCCPaginationError, match="repeating"):
        ECCCClient(session=session).fetch((-66, 43, -59, 48))
    assert len(session.calls) == 1


def test_fetch_stops_at_configured_page_limit() -> None:
    session = FakeSession(
        FakeResponse(
            {
                "type": "FeatureCollection",
                "features": [],
                "links": [{"rel": "next", "href": "?offset=1"}],
            }
        ),
        FakeResponse(
            {
                "type": "FeatureCollection",
                "features": [],
                "links": [{"rel": "next", "href": "?offset=2"}],
            }
        ),
    )

    with pytest.raises(ECCCPaginationError, match="more than 2 pages"):
        ECCCClient(session=session, max_pages=2).fetch((-66, 43, -59, 48))
    assert len(session.calls) == 2


@pytest.mark.parametrize(
    "href",
    [
        "http://api.weather.gc.ca/collections/example/items?offset=1",
        "https://example.com/collections/example/items?offset=1",
        "https://user:password@api.weather.gc.ca/items?offset=1",
    ],
)
def test_fetch_rejects_unsafe_or_cross_origin_pagination(href: str) -> None:
    session = FakeSession(
        FakeResponse(
            {
                "type": "FeatureCollection",
                "features": [],
                "links": [{"rel": "next", "href": href}],
            }
        )
    )
    with pytest.raises(ECCCPaginationError, match="pagination link"):
        ECCCClient(session=session).fetch((-66, 43, -59, 48))
    assert len(session.calls) == 1


@pytest.mark.parametrize(
    ("status_code", "message_fragment"),
    [
        (400, "rejected"),
        (404, "rejected"),
        (429, "limiting"),
        (500, "temporarily unavailable"),
        (503, "temporarily unavailable"),
        (302, "unexpected HTTP status"),
    ],
)
def test_fetch_translates_http_failures(
    status_code: int,
    message_fragment: str,
) -> None:
    session = FakeSession(
        FakeResponse({}, status_code=status_code, content_type="text/plain")
    )
    with pytest.raises(ECCCRequestError, match=message_fragment):
        ECCCClient(session=session).fetch((-66, 43, -59, 48))


@pytest.mark.parametrize(
    ("request_error", "message_fragment"),
    [
        (requests.Timeout("details"), "timed out"),
        (requests.ConnectionError("details"), "Could not connect"),
        (requests.RequestException("details"), "could not be completed"),
    ],
)
def test_fetch_translates_network_failures(
    request_error: requests.RequestException,
    message_fragment: str,
) -> None:
    session = FakeSession(request_error)
    with pytest.raises(ECCCRequestError, match=message_fragment) as exc:
        ECCCClient(session=session).fetch((-66, 43, -59, 48))
    assert "details" not in str(exc.value)


@pytest.mark.parametrize(
    "api_url",
    [
        "",
        "http://api.weather.gc.ca/items",
        "https://user:secret@api.weather.gc.ca/items",
        "not a URL",
    ],
)
def test_client_rejects_unsafe_api_url(api_url: str) -> None:
    with pytest.raises(ECCCConfigurationError, match="API URL"):
        ECCCClient(api_url=api_url, session=FakeSession())


@pytest.mark.parametrize("max_pages", [0, -1, MAX_PAGES + 1, 1.5, True])
def test_client_rejects_invalid_page_limit(max_pages: Any) -> None:
    with pytest.raises(ECCCConfigurationError, match="Page limit"):
        ECCCClient(session=FakeSession(), max_pages=max_pages)


@pytest.mark.parametrize(
    "max_features",
    [0, -1, MAX_TOTAL_FEATURES + 1, 1.5, True],
)
def test_client_rejects_invalid_feature_limit(max_features: Any) -> None:
    with pytest.raises(ECCCConfigurationError, match="Feature limit"):
        ECCCClient(session=FakeSession(), max_features=max_features)


def test_fetch_stops_before_aggregating_too_many_features() -> None:
    session = FakeSession(
        FakeResponse(
            {
                "type": "FeatureCollection",
                "features": [feature("one"), feature("two")],
            }
        )
    )

    with pytest.raises(ECCCResponseError, match="smaller region"):
        ECCCClient(session=session, max_features=1).fetch(
            (-66, 43, -59, 48)
        )


def test_fetch_rejects_malformed_links() -> None:
    session = FakeSession(
        FakeResponse(
            {
                "type": "FeatureCollection",
                "features": [],
                "links": [{"rel": "next"}],
            }
        )
    )
    with pytest.raises(ECCCResponseError, match="without a URL"):
        ECCCClient(session=session).fetch((-66, 43, -59, 48))
