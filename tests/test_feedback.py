from __future__ import annotations

import io
import json
from datetime import datetime, timezone

import pytest
import requests

import coastal_flood_explorer.feedback as feedback_module
from coastal_flood_explorer.feedback import (
    CreatedIssue,
    FeedbackConfigurationError,
    FeedbackSubmissionError,
    GitHubConfig,
    build_issue_body,
    create_github_issue,
    format_issue_title,
    get_app_version,
    get_current_page_context,
    get_deployment_environment,
    github_config_from_secrets,
    sanitize_session_state,
)


class _UnusualValue:
    def __repr__(self) -> str:
        raise AssertionError("The snapshot must not call repr")

    def __str__(self) -> str:
        raise AssertionError("The snapshot must not call str")


class _Response:
    def __init__(
        self,
        status_code: int,
        payload: object,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def json(self) -> object:
        return self._payload


class _HTTPClient:
    def __init__(self, *responses: _Response) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def post(self, url: str, **kwargs: object) -> _Response:
        self.calls.append({"url": url, **kwargs})
        return self.responses.pop(0)


CONFIG = GitHubConfig(
    token="github-token-that-must-stay-private",
    owner="example-owner",
    repo="example-repo",
)


def test_session_state_sanitization_removes_sensitive_keys_recursively() -> None:
    state = {
        "selected_station": "00491",
        "github_token": "top-secret",
        "nested": {
            "password": "also-secret",
            "AuthorizationHeader": "Bearer hidden",
            "safe": 3,
        },
        "apiKey": "hidden-key",
    }

    snapshot = sanitize_session_state(state)
    encoded = json.dumps(snapshot)

    assert snapshot["selected_station"] == "00491"
    assert snapshot["nested"] == {"safe": 3}
    assert "top-secret" not in encoded
    assert "also-secret" not in encoded
    assert "hidden-key" not in encoded
    assert "github_token" not in encoded
    assert snapshot["__snapshot_metadata__"][
        "sensitive_values_omitted"
    ] == 4


def test_session_state_sanitization_handles_unusual_values_and_cycles() -> None:
    cycle: list[object] = []
    cycle.append(cycle)
    snapshot = sanitize_session_state(
        {
            "when": datetime(2026, 7, 28, 15, 0, tzinfo=timezone.utc),
            "coordinates": (1.25, 2.5),
            "not_a_number": float("nan"),
            "binary": b"private bytes",
            "upload": io.BytesIO(b"uploaded file contents"),
            "unusual": _UnusualValue(),
            "cycle": cycle,
        }
    )

    assert snapshot["when"] == "2026-07-28T15:00:00+00:00"
    assert snapshot["coordinates"] == [1.25, 2.5]
    assert snapshot["not_a_number"] == "nan"
    assert snapshot["binary"].startswith("<omitted binary value:")
    assert snapshot["upload"].startswith("<omitted uploaded file:")
    assert "uploaded file contents" not in json.dumps(snapshot)
    assert snapshot["unusual"].endswith("._UnusualValue>")
    assert snapshot["cycle"] == ["<omitted cyclic reference>"]
    json.dumps(snapshot, allow_nan=False)


def test_session_state_snapshot_has_a_hard_total_size_limit() -> None:
    state = {f"value_{index}": "x" * 1_000 for index in range(20)}

    snapshot = sanitize_session_state(state, max_bytes=400)
    encoded = json.dumps(
        snapshot,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")

    assert len(encoded) <= 400
    assert snapshot["__snapshot_metadata__"]["truncated"] is True
    assert snapshot["__snapshot_metadata__"]["size_limited_values"] > 0


def test_current_page_query_parameters_are_sanitized() -> None:
    context = get_current_page_context(
        "  Main   explorer ",
        {"station": "00491", "access_token": "do-not-copy"},
    )

    assert context["current_page"] == "Main explorer"
    assert context["query_parameters"]["station"] == "00491"
    assert "do-not-copy" not in json.dumps(context)


def test_issue_title_is_normalized_prefixed_and_bounded() -> None:
    assert format_issue_title(
        "Bug",
        "  Graph disappears\n after changing material  ",
    ) == "[Bug] Graph disappears after changing material"
    assert format_issue_title("General feedback", "Useful map") == (
        "[Feedback] Useful map"
    )
    assert len(format_issue_title("Suggestion", "x" * 400)) <= 240


def test_markdown_issue_body_contains_context_and_optional_state() -> None:
    body = build_issue_body(
        comment="The graph disappeared after I changed the layer.",
        contact="Analyst <analyst@example.test>",
        report_type="Bug",
        submitted_at=datetime(2026, 7, 28, 15, 30, tzinfo=timezone.utc),
        current_page="Geo Stream Coastal Flood Explorer",
        query_parameters={"station": "00491"},
        app_version="abc1234",
        deployment_environment="Streamlit Community Cloud",
        report_id="report-123",
        state_snapshot={"selected_station": "00491"},
    )

    assert "## User report" in body
    assert "The graph disappeared" in body
    assert "## Report context" in body
    assert "2026-07-28T15:30:00Z" in body
    assert "Geo Stream Coastal Flood Explorer" in body
    assert "abc1234" in body
    assert "report-123" in body
    assert "## Current app state" in body
    assert '"selected_station": "00491"' in body


def test_markdown_issue_body_omits_state_without_consent() -> None:
    body = build_issue_body(
        comment="A general note.",
        report_type="General feedback",
        submitted_at=datetime(2026, 7, 28, tzinfo=timezone.utc),
        current_page="Main",
        query_parameters={},
        app_version="unknown",
        deployment_environment="Local",
        report_id="report-456",
    )

    assert "## Current app state" not in body


def test_successful_github_response_returns_public_issue_details() -> None:
    client = _HTTPClient(
        _Response(
            201,
            {
                "number": 42,
                "html_url": "https://untrusted.example/ignored",
            },
        )
    )

    issue = create_github_issue(
        CONFIG,
        title="[Bug] Missing graph",
        body="Report body",
        label="bug",
        http_client=client,
    )

    assert issue == CreatedIssue(
        number=42,
        url="https://github.com/example-owner/example-repo/issues/42",
        label_applied=True,
    )
    assert client.calls[0]["json"]["labels"] == ["bug"]
    assert client.calls[0]["timeout"] == (3.05, 12.0)


def test_unavailable_label_is_retried_without_failing_submission() -> None:
    client = _HTTPClient(
        _Response(422, {"message": "Validation Failed"}),
        _Response(201, {"number": 43}),
    )

    issue = create_github_issue(
        CONFIG,
        title="[Suggestion] Add a layer",
        body="Report body",
        label="enhancement",
        http_client=client,
    )

    assert issue.number == 43
    assert issue.label_applied is False
    assert "labels" in client.calls[0]["json"]
    assert "labels" not in client.calls[1]["json"]


def test_github_authentication_failure_is_safe() -> None:
    client = _HTTPClient(_Response(401, {"message": "Bad credentials"}))

    with pytest.raises(FeedbackSubmissionError) as exc_info:
        create_github_issue(
            CONFIG,
            title="[Bug] Missing graph",
            body="Report body",
            label="bug",
            http_client=client,
        )

    assert "credentials or permissions" in str(exc_info.value)
    assert CONFIG.token not in str(exc_info.value)


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (
            _Response(403, {}, headers={"X-RateLimit-Remaining": "0"}),
            "rate-limiting",
        ),
        (_Response(503, {}), "temporarily unavailable"),
    ],
)
def test_github_rate_limit_and_server_failures_are_safe(
    response: _Response,
    message: str,
) -> None:
    with pytest.raises(FeedbackSubmissionError, match=message):
        create_github_issue(
            CONFIG,
            title="[Feedback] Note",
            body="Report body",
            label="feedback",
            http_client=_HTTPClient(response),
        )


def test_github_network_failure_is_safe() -> None:
    class FailingClient:
        def post(self, *args: object, **kwargs: object) -> None:
            raise requests.ConnectionError("offline")

    with pytest.raises(FeedbackSubmissionError, match="could not reach"):
        create_github_issue(
            CONFIG,
            title="[Feedback] Note",
            body="Report body",
            label="feedback",
            http_client=FailingClient(),
        )


def test_missing_streamlit_secret_configuration_is_safe() -> None:
    with pytest.raises(FeedbackConfigurationError) as exc_info:
        github_config_from_secrets({})

    assert "not configured" in str(exc_info.value)


def test_app_version_prefers_explicit_commit_environment() -> None:
    assert get_app_version(
        environment={
            "GEO_STREAM_COMMIT_SHA": "first-commit",
            "GITHUB_SHA": "deployment-commit",
        }
    ) == "first-commit"


def test_app_version_falls_back_to_git_and_then_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class GitResult:
        returncode = 0
        stdout = "d99ef44aabbccddeeff0011223344556677889900\n"

    monkeypatch.setattr(
        feedback_module.subprocess,
        "run",
        lambda *args, **kwargs: GitResult(),
    )
    assert get_app_version(environment={}) == GitResult.stdout.strip()

    def unavailable_git(*args: object, **kwargs: object) -> None:
        raise OSError("git is unavailable")

    monkeypatch.setattr(
        feedback_module.subprocess,
        "run",
        unavailable_git,
    )
    assert get_app_version(environment={}) == "unknown"


def test_deployment_environment_uses_the_browser_app_url() -> None:
    assert get_deployment_environment(
        {},
        current_url="https://geo-stream-example.streamlit.app/path",
    ) == "Streamlit Community Cloud"
    assert get_deployment_environment(
        {},
        current_url="http://localhost:8501",
    ) == "Local"
