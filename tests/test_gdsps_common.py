"""Unit tests for GDSPS shared types and helpers."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from coastal_flood_explorer.api import ECCCError
from coastal_flood_explorer.gdsps_common import (
    ETAS,
    GDSPS_MODEL,
    GDSPS_VARIABLES,
    RESPS_MODEL,
    SSH,
    GDSPSConfigurationError,
    GDSPSDatamartFile,
    GDSPSError,
    GDSPSLayerInfo,
    GDSPSResponseError,
    GDSPSRun,
    classify_model,
    classify_variable,
    is_gdsps_identifier,
    normalize_variable,
    parse_iso_utc,
    resps_member,
    utc_text,
    validate_bbox,
    variable_definition,
)


def test_gdsps_error_is_in_eccc_family() -> None:
    assert issubclass(GDSPSError, ECCCError)
    assert issubclass(GDSPSConfigurationError, GDSPSError)


def test_variables_are_etas_and_ssh() -> None:
    assert GDSPS_VARIABLES == (ETAS, SSH) == ("ETAS", "SSH")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("GDSPS.ETAS", True),
        ("some_storm-surge_layer", True),
        ("HRDPS.SSH", True),
        ("GDPS.ETA_TT", False),
        ("random", False),
        (None, False),
    ],
)
def test_is_gdsps_identifier(value: str | None, expected: bool) -> None:
    assert is_gdsps_identifier(value) is expected


@pytest.mark.parametrize(
    ("candidates", "expected"),
    [
        (("GDSPS.ETAS",), ETAS),
        (("storm surge elevation",), ETAS),
        (("GDSPS.SSH",), SSH),
        (("total water level",), SSH),
        (("nothing", "here"), None),
        # ETAS wins over SSH when both appear because it is more specific.
        (("ETAS and SSH both",), ETAS),
    ],
)
def test_classify_variable(candidates: tuple[str, ...], expected: str | None) -> None:
    assert classify_variable(*candidates) == expected


@pytest.mark.parametrize(
    ("candidates", "expected"),
    [
        # GDSPS is identified by its acronym, never by the bare phenomenon.
        (("GDSPS_15km_StormSurge",), GDSPS_MODEL),
        (("GDSPS_15km_SeaSfcHeight", "GDSPS.SSH - ..."), GDSPS_MODEL),
        # RESPS is a different model and must classify as RESPS, not GDSPS.
        (("RESPS-Atlantic-North-West_9km_StormSurge_01",), RESPS_MODEL),
        (("some_name", "Regional Ensemble ... (RESPS)"), RESPS_MODEL),
        # Bare "storm surge" legend styles / group containers name no model.
        (("Storm_Surge-Dis",), None),
        (("StormSurge_-3-3",), None),
        (("Storm surge legend",), None),
        ((None, 123), None),
    ],
)
def test_classify_model_separates_models_and_rejects_non_model(
    candidates: tuple[object, ...],
    expected: str | None,
) -> None:
    assert classify_model(*candidates) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("RESPS-Atlantic-North-West_9km_StormSurge_01", 1),
        ("RESPS-Atlantic-North-West_9km_SeaSfcHeight_21", 21),
        ("GDSPS_15km_StormSurge", None),
        ("no_trailing_number", None),
        (None, None),
    ],
)
def test_resps_member_parsing(value: str | None, expected: int | None) -> None:
    assert resps_member(value) == expected


def test_normalize_variable_is_case_insensitive() -> None:
    assert normalize_variable("etas") == ETAS
    assert normalize_variable("  ssh ") == SSH
    assert normalize_variable("bogus") is None
    assert normalize_variable(3) is None


def test_variable_definition_distinguishes_etas_from_ssh() -> None:
    etas = variable_definition("etas")
    ssh = variable_definition("SSH")
    assert "surge" in etas.lower()
    assert "not an engineering" in ssh.lower() or "not a" in ssh.lower()
    assert etas != ssh
    with pytest.raises(GDSPSConfigurationError):
        variable_definition("bogus")


def test_utc_text_and_parse_round_trip() -> None:
    moment = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
    text = utc_text(moment)
    assert text == "2026-07-22T12:00:00Z"
    assert parse_iso_utc(text) == moment


def test_parse_iso_utc_treats_naive_as_utc() -> None:
    assert parse_iso_utc("2026-07-22T00:00:00") == datetime(
        2026, 7, 22, tzinfo=timezone.utc
    )


def test_parse_iso_utc_rejects_garbage() -> None:
    with pytest.raises(GDSPSResponseError):
        parse_iso_utc("not-a-date")
    with pytest.raises(GDSPSResponseError):
        parse_iso_utc("")


@pytest.mark.parametrize(
    "bbox",
    [
        (-63.0, 44.0, -62.0, 45.0),
        [-63.0, 44.0, -62.0, 45.0],
    ],
)
def test_validate_bbox_accepts_ordered_boxes(bbox: object) -> None:
    assert validate_bbox(bbox) == (-63.0, 44.0, -62.0, 45.0)


@pytest.mark.parametrize(
    "bbox",
    [
        "not a box",
        (1.0, 2.0, 3.0),
        (-200.0, 44.0, -62.0, 45.0),
        (-62.0, 44.0, -63.0, 45.0),
        (-63.0, 44.0, -62.0, float("nan")),
        (True, 44.0, -62.0, 45.0),
    ],
)
def test_validate_bbox_rejects_invalid(bbox: object) -> None:
    with pytest.raises(GDSPSConfigurationError):
        validate_bbox(bbox)


def test_run_and_file_metadata_are_json_ready() -> None:
    run = GDSPSRun(
        issue_time=datetime(2026, 7, 22, 0, tzinfo=timezone.utc),
        cycle="00",
    )
    assert run.label == "2026-07-22 00Z"
    assert run.stamp == "20260722T00Z"
    layer = GDSPSLayerInfo(
        name="GDSPS.ETAS",
        title="Storm surge",
        variable=ETAS,
        available_times=(datetime(2026, 7, 22, 1, tzinfo=timezone.utc),),
    )
    assert layer.metadata()["available_times"] == ["2026-07-22T01:00:00Z"]
    file = GDSPSDatamartFile(
        filename="x.nc",
        url="https://dd.weather.gc.ca/model_gdsps/x.nc",
        variable=ETAS,
        run=run,
        lead_hours=3,
        valid_time=datetime(2026, 7, 22, 3, tzinfo=timezone.utc),
    )
    meta = file.metadata()
    assert meta["variable"] == "ETAS"
    assert meta["run"]["cycle"] == "00"
    assert meta["valid_time"] == "2026-07-22T03:00:00Z"
