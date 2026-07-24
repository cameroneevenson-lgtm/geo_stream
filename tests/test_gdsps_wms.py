"""Unit tests for the GDSPS GeoMet WMS discovery client."""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from typing import Any

import pytest

from coastal_flood_explorer.gdsps_common import (
    ETAS,
    SSH,
    GDSPSConfigurationError,
    GDSPSDiscoveryError,
    GDSPSLayerInfo,
    GDSPSRequestError,
)
from coastal_flood_explorer.gdsps_wms import (
    GDSPSWMSClient,
    build_wms_tile_params,
)

NS = 'xmlns="http://www.opengis.net/wms"'


def capabilities(*layers: str) -> str:
    inner = "".join(layers)
    return (
        f'<?xml version="1.0"?><WMS_Capabilities {NS} version="1.3.0">'
        f"<Capability><Layer><Title>Root</Title>{inner}</Layer></Capability>"
        "</WMS_Capabilities>"
    )


def layer_xml(name: str, title: str, dimension: str | None = None) -> str:
    dim = (
        f'<Dimension name="time" units="ISO8601">{dimension}</Dimension>'
        if dimension is not None
        else ""
    )
    return f"<Layer><Name>{name}</Name><Title>{title}</Title>{dim}</Layer>"


class FakeResponse:
    def __init__(
        self,
        *,
        text: str = "",
        status_code: int = 200,
        content_type: str = "text/xml",
    ) -> None:
        self.text = text
        self.content = text.encode("utf-8")
        self.status_code = status_code
        self.headers = {"Content-Type": content_type}


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


def test_discovers_gdsps_layers_and_times() -> None:
    document = capabilities(
        layer_xml(
            "GDSPS.ETAS",
            "Storm surge elevation",
            "2026-07-22T00:00:00Z,2026-07-22T01:00:00Z",
        ),
        layer_xml(
            "GDSPS.SSH",
            "Total water level",
            "2026-07-22T00:00:00Z/2026-07-22T03:00:00Z/PT1H",
        ),
        layer_xml("GDPS.ETA_TT", "Air temperature"),
    )
    session = FakeSession(FakeResponse(text=document))
    client = GDSPSWMSClient(session=session)
    layers = client.discover_layers()

    assert [layer.name for layer in layers] == ["GDSPS.ETAS", "GDSPS.SSH"]
    etas, ssh = layers
    assert etas.variable == ETAS
    assert ssh.variable == SSH
    assert etas.available_times == (
        datetime(2026, 7, 22, 0, tzinfo=timezone.utc),
        datetime(2026, 7, 22, 1, tzinfo=timezone.utc),
    )
    # The interval start/end/PT1H expands inclusively to four hourly steps.
    assert len(ssh.available_times) == 4
    assert ssh.available_times[-1] == datetime(
        2026, 7, 22, 3, tzinfo=timezone.utc
    )
    # A GET-only retry session and the GeoMet endpoint were used.
    (url, kwargs) = session.calls[0]
    assert url == "https://geo.weather.gc.ca/geomet"
    assert kwargs["params"]["request"] == "GetCapabilities"
    assert kwargs["allow_redirects"] is False


def test_no_matching_layers_is_empty_not_error() -> None:
    document = capabilities(layer_xml("GDPS.ETA_TT", "Air temperature"))
    client = GDSPSWMSClient(session=FakeSession(FakeResponse(text=document)))
    assert client.discover_layers() == ()


def test_non_xml_content_type_is_rejected() -> None:
    client = GDSPSWMSClient(
        session=FakeSession(FakeResponse(text="<html/>", content_type="text/html"))
    )
    with pytest.raises(GDSPSDiscoveryError):
        client.discover_layers()


def test_http_error_status_raises_request_error() -> None:
    client = GDSPSWMSClient(session=FakeSession(FakeResponse(status_code=503)))
    with pytest.raises(GDSPSRequestError):
        client.discover_layers()


def test_build_wms_tile_params_validates_and_formats() -> None:
    layer = GDSPSLayerInfo(
        name="GDSPS.ETAS",
        title="Storm surge",
        variable=ETAS,
        available_times=(datetime(2026, 7, 22, 1, tzinfo=timezone.utc),),
    )
    params = build_wms_tile_params(
        layer,
        time=datetime(2026, 7, 22, 1, tzinfo=timezone.utc),
        opacity=0.5,
    )
    assert params["layers"] == "GDSPS.ETAS"
    assert params["url"] == "https://geo.weather.gc.ca/geomet"
    assert params["time"] == "2026-07-22T01:00:00Z"
    assert params["opacity"] == 0.5
    assert params["transparent"] is True
    assert params["variable"] == ETAS

    no_time = build_wms_tile_params(layer, opacity=1.0)
    assert no_time["time"] is None


@pytest.mark.parametrize("opacity", [-0.1, 1.5, True, "x"])
def test_build_wms_tile_params_rejects_bad_opacity(opacity: object) -> None:
    layer = GDSPSLayerInfo(name="GDSPS.ETAS", title="t", variable=ETAS)
    with pytest.raises(GDSPSConfigurationError):
        build_wms_tile_params(layer, opacity=opacity)  # type: ignore[arg-type]


def test_invalid_endpoint_is_rejected() -> None:
    with pytest.raises(GDSPSConfigurationError):
        GDSPSWMSClient(endpoint="http://insecure.example/geomet")
