"""Safe feedback-report construction and GitHub Issue submission."""

from __future__ import annotations

import io
import json
import itertools
import math
import os
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests


GITHUB_API_ROOT = "https://api.github.com"
GITHUB_API_VERSION = "2026-03-10"
DEFAULT_SNAPSHOT_MAX_BYTES = 12_000
MAX_COLLECTION_ITEMS = 50
MAX_SNAPSHOT_DEPTH = 5
MAX_SNAPSHOT_STRING_CHARS = 1_000
REPORT_LABELS = {
    "Bug": "bug",
    "Suggestion": "enhancement",
    "General feedback": "feedback",
}
SENSITIVE_KEY_TERMS = (
    "token",
    "secret",
    "password",
    "credential",
    "authorization",
    "cookie",
    "key",
)
_COMMIT_ENVIRONMENT_VARIABLES = (
    "GEO_STREAM_COMMIT_SHA",
    "GIT_COMMIT_SHA",
    "COMMIT_SHA",
)
_DEPLOYMENT_VERSION_VARIABLES = (
    "GITHUB_SHA",
    "STREAMLIT_GIT_COMMIT",
    "STREAMLIT_COMMIT_SHA",
    "RENDER_GIT_COMMIT",
    "SOURCE_VERSION",
    "HEROKU_SLUG_COMMIT",
)


class FeedbackError(Exception):
    """Base class for errors that are safe to display to an app user."""


class FeedbackConfigurationError(FeedbackError):
    """Raised when the GitHub integration is not configured safely."""


class FeedbackSubmissionError(FeedbackError):
    """Raised when GitHub does not accept a feedback report."""


@dataclass(frozen=True, slots=True)
class GitHubConfig:
    """Credentials and repository coordinates for issue creation."""

    token: str
    owner: str
    repo: str


@dataclass(frozen=True, slots=True)
class CreatedIssue:
    """Public details returned after a GitHub Issue is created."""

    number: int
    url: str
    label_applied: bool


def _clean_config_value(value: object) -> str:
    """Return a stripped scalar configuration value without coercing objects."""

    return value.strip() if isinstance(value, str) else ""


def github_config_from_secrets(secrets: object) -> GitHubConfig:
    """Read the required ``[github]`` section from Streamlit-like secrets."""

    try:
        github = secrets["github"]  # type: ignore[index]
        token = _clean_config_value(github["token"])
        owner = _clean_config_value(github["owner"])
        repo = _clean_config_value(github["repo"])
    except Exception as exc:
        raise FeedbackConfigurationError(
            "Feedback is not configured for this deployment. Please contact "
            "the app administrator."
        ) from exc

    if not token or not owner or not repo:
        raise FeedbackConfigurationError(
            "Feedback is not configured for this deployment. Please contact "
            "the app administrator."
        )
    if not _valid_repository_part(owner) or not _valid_repository_part(repo):
        raise FeedbackConfigurationError(
            "The feedback repository configuration is invalid. Please "
            "contact the app administrator."
        )
    return GitHubConfig(token=token, owner=owner, repo=repo)


def _valid_repository_part(value: str) -> bool:
    """Return whether a repository path component is safe for an API URL."""

    return bool(re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", value))


def _is_sensitive_key(key: str) -> bool:
    """Return whether a state key may identify a sensitive value."""

    normalized = key.casefold()
    return any(term in normalized for term in SENSITIVE_KEY_TERMS)


def _is_uploaded_file(value: object) -> bool:
    """Recognize Streamlit and file-like uploads without reading them."""

    type_name = type(value).__name__.casefold()
    module_name = type(value).__module__.casefold()
    return (
        "uploadedfile" in type_name
        or "uploaded_file" in type_name
        or "streamlit.runtime.uploaded_file_manager" in module_name
        or isinstance(value, io.IOBase)
    )


def _safe_type_name(value: object) -> str:
    """Describe an unsupported value without invoking its string methods."""

    value_type = type(value)
    module = value_type.__module__
    name = value_type.__qualname__
    return name if module == "builtins" else f"{module}.{name}"


def _json_size(value: object) -> int:
    """Return the UTF-8 size of a compact JSON representation."""

    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )


def _safe_snapshot_value(
    value: object,
    *,
    depth: int,
    seen: set[int],
    stats: dict[str, int],
) -> object:
    """Convert one state value to bounded JSON-safe data."""

    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, str):
        if len(value) <= MAX_SNAPSHOT_STRING_CHARS:
            return value
        stats["strings_truncated"] += 1
        omitted = len(value) - MAX_SNAPSHOT_STRING_CHARS
        return (
            value[:MAX_SNAPSHOT_STRING_CHARS]
            + f"… <{omitted} character(s) truncated>"
        )
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        stats["values_omitted"] += 1
        return f"<omitted binary value: {_safe_type_name(value)}>"
    if _is_uploaded_file(value):
        stats["values_omitted"] += 1
        return f"<omitted uploaded file: {_safe_type_name(value)}>"
    if depth >= MAX_SNAPSHOT_DEPTH:
        stats["values_omitted"] += 1
        return f"<omitted beyond depth limit: {_safe_type_name(value)}>"

    value_id = id(value)
    if isinstance(value, Mapping):
        if value_id in seen:
            stats["values_omitted"] += 1
            return "<omitted cyclic reference>"
        seen.add(value_id)
        result: dict[str, object] = {}
        try:
            items = list(
                itertools.islice(value.items(), MAX_COLLECTION_ITEMS + 1)
            )
            for index, (raw_key, item) in enumerate(items):
                if index >= MAX_COLLECTION_ITEMS:
                    stats["values_omitted"] += 1
                    break
                if not isinstance(raw_key, str):
                    stats["values_omitted"] += 1
                    continue
                if _is_sensitive_key(raw_key):
                    stats["sensitive_values_omitted"] += 1
                    continue
                result[raw_key] = _safe_snapshot_value(
                    item,
                    depth=depth + 1,
                    seen=seen,
                    stats=stats,
                )
        finally:
            seen.remove(value_id)
        return result

    if isinstance(value, (list, tuple, set, frozenset)):
        if value_id in seen:
            stats["values_omitted"] += 1
            return "<omitted cyclic reference>"
        seen.add(value_id)
        try:
            values = list(itertools.islice(value, MAX_COLLECTION_ITEMS + 1))
            result_list = [
                _safe_snapshot_value(
                    item,
                    depth=depth + 1,
                    seen=seen,
                    stats=stats,
                )
                for item in values[:MAX_COLLECTION_ITEMS]
            ]
            if len(values) > MAX_COLLECTION_ITEMS:
                stats["values_omitted"] += 1
        finally:
            seen.remove(value_id)
        return result_list

    stats["values_omitted"] += 1
    return f"<unsupported value: {_safe_type_name(value)}>"


def sanitize_session_state(
    state: Mapping[object, object],
    *,
    max_bytes: int = DEFAULT_SNAPSHOT_MAX_BYTES,
    excluded_prefixes: tuple[str, ...] = (),
) -> dict[str, object]:
    """Return a bounded, JSON-safe snapshot with sensitive state removed."""

    if max_bytes < 32:
        raise ValueError("max_bytes must be at least 32")
    stats = {
        "sensitive_values_omitted": 0,
        "values_omitted": 0,
        "strings_truncated": 0,
        "size_limited_values": 0,
    }
    converted: dict[str, object] = {}
    state_items = itertools.islice(state.items(), 201)
    for index, (raw_key, value) in enumerate(state_items):
        if index >= 200:
            stats["values_omitted"] += 1
            break
        if not isinstance(raw_key, str):
            stats["values_omitted"] += 1
            continue
        if raw_key == "__snapshot_metadata__":
            stats["values_omitted"] += 1
            continue
        if _is_sensitive_key(raw_key):
            stats["sensitive_values_omitted"] += 1
            continue
        if any(raw_key.startswith(prefix) for prefix in excluded_prefixes):
            stats["values_omitted"] += 1
            continue
        safe_value = _safe_snapshot_value(
            value,
            depth=0,
            seen=set(),
            stats=stats,
        )
        candidate = {**converted, raw_key: safe_value}
        if _json_size(candidate) <= max_bytes:
            converted[raw_key] = safe_value
        else:
            stats["size_limited_values"] += 1

    def metadata() -> dict[str, object]:
        return {
            "truncated": bool(
                stats["values_omitted"]
                or stats["strings_truncated"]
                or stats["size_limited_values"]
            ),
            **{key: value for key, value in stats.items() if value},
        }

    if any(stats.values()):
        converted["__snapshot_metadata__"] = metadata()
        normal_keys = [
            key for key in converted if key != "__snapshot_metadata__"
        ]
        while _json_size(converted) > max_bytes and normal_keys:
            converted.pop(normal_keys.pop())
            stats["size_limited_values"] += 1
            converted["__snapshot_metadata__"] = metadata()
        if _json_size(converted) > max_bytes:
            converted = {"__snapshot_truncated__": True}
    return converted


def format_issue_title(report_type: str, short_title: str) -> str:
    """Build a concise, single-line GitHub Issue title."""

    if report_type not in REPORT_LABELS:
        raise ValueError("Unsupported report type")
    cleaned = " ".join(short_title.split())
    if not cleaned:
        raise ValueError("A short title is required")
    prefix = "Feedback" if report_type == "General feedback" else report_type
    available = 240 - len(prefix) - 3
    return f"[{prefix}] {cleaned[:available].rstrip()}"


def get_current_page_context(
    current_page: str,
    query_parameters: Mapping[object, object] | None = None,
) -> dict[str, object]:
    """Build safe navigation context for a report."""

    page = " ".join(current_page.split())[:200] or "Unknown"
    safe_query = sanitize_session_state(
        query_parameters or {},
        max_bytes=2_000,
    )
    return {
        "current_page": page,
        "query_parameters": safe_query,
    }


def get_app_version(
    *,
    environment: Mapping[str, str] | None = None,
    repository_directory: str | Path | None = None,
) -> str:
    """Identify deployed code without requiring Git metadata."""

    env = os.environ if environment is None else environment
    for variable in (
        *_COMMIT_ENVIRONMENT_VARIABLES,
        *_DEPLOYMENT_VERSION_VARIABLES,
    ):
        value = env.get(variable, "").strip()
        if value:
            return " ".join(value.split())[:200]

    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repository_directory,
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    commit = result.stdout.strip()
    if result.returncode == 0 and re.fullmatch(r"[0-9a-fA-F]{7,64}", commit):
        return commit
    return "unknown"


def get_deployment_environment(
    environment: Mapping[str, str] | None = None,
    *,
    current_url: str | None = None,
) -> str:
    """Distinguish local execution from common hosted deployments."""

    env = os.environ if environment is None else environment
    explicit = env.get("GEO_STREAM_DEPLOYMENT_ENV", "").strip()
    if explicit:
        return " ".join(explicit.split())[:200]
    if current_url:
        try:
            hostname = (urlparse(current_url).hostname or "").casefold()
        except ValueError:
            hostname = ""
        if hostname == "streamlit.app" or hostname.endswith(".streamlit.app"):
            return "Streamlit Community Cloud"
        if hostname in {"localhost", "127.0.0.1", "::1"}:
            return "Local"
    cloud_markers = (
        env.get("STREAMLIT_CLOUD", ""),
        env.get("STREAMLIT_SHARING_MODE", ""),
        env.get("STREAMLIT_RUNTIME_ENV", ""),
    )
    if any("cloud" in value.casefold() for value in cloud_markers):
        return "Streamlit Community Cloud"
    if any(env.get(variable) for variable in _DEPLOYMENT_VERSION_VARIABLES):
        return "Hosted deployment"
    return "Local"


def _single_line(value: object, *, limit: int = 1_000) -> str:
    """Render bounded report metadata on one Markdown line."""

    return " ".join(str(value).split())[:limit] or "unknown"


def build_issue_body(
    *,
    comment: str,
    report_type: str,
    submitted_at: datetime,
    current_page: str,
    query_parameters: Mapping[str, object],
    app_version: str,
    deployment_environment: str,
    report_id: str,
    contact: str = "",
    state_snapshot: Mapping[str, object] | None = None,
) -> str:
    """Generate a readable Markdown body for one feedback report."""

    clean_comment = comment.strip()
    if not clean_comment:
        raise ValueError("A detailed comment is required")
    if report_type not in REPORT_LABELS:
        raise ValueError("Unsupported report type")
    if submitted_at.tzinfo is None:
        submitted_at = submitted_at.replace(tzinfo=timezone.utc)
    timestamp = submitted_at.astimezone(timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )
    query_json = json.dumps(
        query_parameters,
        ensure_ascii=False,
        separators=(", ", ": "),
        sort_keys=True,
    ).replace("`", "\\u0060")
    parts = [
        "## User report",
        "",
        clean_comment[:10_000],
    ]
    clean_contact = contact.strip()
    if clean_contact:
        parts.extend(("", f"**Name or contact:** {clean_contact[:500]}"))
    parts.extend(
        (
            "",
            "## Report context",
            "",
            f"- **Report type:** {_single_line(report_type)}",
            f"- **Submitted at:** {timestamp}",
            f"- **Current page:** {_single_line(current_page)}",
            "- **Current URL query parameters:** "
            f"`{_single_line(query_json, limit=2_000)}`",
            f"- **App version / Git commit:** {_single_line(app_version)}",
            "- **Deployment environment:** "
            f"{_single_line(deployment_environment)}",
            f"- **Report ID:** {_single_line(report_id, limit=200)}",
        )
    )
    if state_snapshot is not None:
        state_json = json.dumps(
            state_snapshot,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).replace("```", "\\u0060\\u0060\\u0060")
        parts.extend(
            (
                "",
                "## Current app state",
                "",
                "```json",
                state_json,
                "```",
            )
        )
    return "\n".join(parts) + "\n"


def _safe_created_issue(
    response: object,
    config: GitHubConfig,
    *,
    label_applied: bool,
) -> CreatedIssue:
    """Validate the minimal successful GitHub response fields."""

    try:
        payload = response.json()  # type: ignore[attr-defined]
    except (AttributeError, ValueError) as exc:
        raise FeedbackSubmissionError(
            "GitHub created a response that the app could not verify. Please "
            "check the repository before submitting again."
        ) from exc
    number = payload.get("number") if isinstance(payload, Mapping) else None
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        raise FeedbackSubmissionError(
            "GitHub created a response that the app could not verify. Please "
            "check the repository before submitting again."
        )
    url = f"https://github.com/{config.owner}/{config.repo}/issues/{number}"
    return CreatedIssue(
        number=number,
        url=url,
        label_applied=label_applied,
    )


def _raise_for_failed_response(response: object) -> None:
    """Raise a safe user-facing error for a failed GitHub response."""

    status = getattr(response, "status_code", 0)
    headers = getattr(response, "headers", {})
    remaining = headers.get("X-RateLimit-Remaining") if isinstance(
        headers, Mapping
    ) else None
    if status in (429,) or (status == 403 and remaining == "0"):
        raise FeedbackSubmissionError(
            "GitHub is temporarily rate-limiting feedback submissions. "
            "Please try again later."
        )
    if status in (401, 403):
        raise FeedbackSubmissionError(
            "GitHub rejected the feedback integration credentials or "
            "permissions. Please contact the app administrator."
        )
    if status == 404:
        raise FeedbackSubmissionError(
            "The configured GitHub repository could not be accessed. Please "
            "contact the app administrator."
        )
    if isinstance(status, int) and status >= 500:
        raise FeedbackSubmissionError(
            "GitHub is temporarily unavailable. Please try again later."
        )
    raise FeedbackSubmissionError(
        "GitHub did not accept the feedback report. Please review the form "
        "and try again."
    )


def create_github_issue(
    config: GitHubConfig,
    *,
    title: str,
    body: str,
    label: str | None,
    http_client: object = requests,
    timeout: tuple[float, float] = (3.05, 12.0),
) -> CreatedIssue:
    """Create a GitHub Issue, retrying without an unavailable label."""

    url = f"{GITHUB_API_ROOT}/repos/{config.owner}/{config.repo}/issues"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {config.token}",
        "User-Agent": "geo-stream-feedback",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
    }
    payload: dict[str, object] = {"title": title, "body": body}
    if label:
        payload["labels"] = [label]

    def post(issue_payload: Mapping[str, object]) -> object:
        try:
            return http_client.post(  # type: ignore[attr-defined]
                url,
                headers=headers,
                json=dict(issue_payload),
                timeout=timeout,
            )
        except requests.RequestException as exc:
            raise FeedbackSubmissionError(
                "The feedback service could not reach GitHub. Please try "
                "again later."
            ) from exc

    response = post(payload)
    status = getattr(response, "status_code", 0)
    if status == 201:
        return _safe_created_issue(
            response,
            config,
            label_applied=bool(label),
        )
    if status == 422 and label:
        response = post({"title": title, "body": body})
        if getattr(response, "status_code", 0) == 201:
            return _safe_created_issue(
                response,
                config,
                label_applied=False,
            )
    _raise_for_failed_response(response)
    raise AssertionError("Unreachable")
