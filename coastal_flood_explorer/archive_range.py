"""Pure aggregation helpers for inclusive ECCC archive date ranges.

The single-date archive client remains the network and validation boundary.
This module lets callers fetch those dates directly, or combine independently
cached single-date results while retaining a diagnostic for every requested
day.  A missing archive partition therefore does not discard other successful
days.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TypeAlias

from .api import MAX_TOTAL_FEATURES
from .archive import (
    MAX_ARCHIVE_FILES,
    ArchiveDocument,
    ArchiveError,
    ArchiveFetchResult,
    ArchiveProduct,
    ECCCDatamartArchiveClient,
    ECCCArchiveConfigurationError,
    ECCCArchiveRequestError,
    ECCCArchiveResponseError,
    validate_archive_date,
)
from .archive_dates import ARCHIVE_RETENTION_DAYS
from .properties import json_safe

ArchiveDateLike: TypeAlias = str | date
ArchiveSuccess: TypeAlias = tuple[ArchiveDateLike, ArchiveFetchResult]
ArchiveFailure: TypeAlias = tuple[ArchiveDateLike, ArchiveError | str]

MAX_ARCHIVE_RANGE_DAYS = ARCHIVE_RETENTION_DAYS


class ArchiveRangeValidationError(ECCCArchiveConfigurationError, ValueError):
    """Raised when an archive range or its outcomes are inconsistent."""


@dataclass(frozen=True, slots=True)
class ArchiveDateOutcome:
    """One requested date's copied result or safe failure diagnostic."""

    issue_date: str
    result: ArchiveFetchResult | None = None
    error_type: str | None = None
    error_message: str | None = None

    @property
    def succeeded(self) -> bool:
        """Return whether this date produced a validated fetch result."""

        return self.result is not None

    @property
    def feature_count(self) -> int:
        """Return the number of merged features loaded for this date."""

        if self.result is None:
            return 0
        return len(self.result.collection["features"])

    @property
    def product_count(self) -> int:
        """Return the number of selected products loaded for this date."""

        if self.result is None:
            return 0
        return len(self.result.products)


@dataclass(frozen=True, slots=True)
class ArchiveRangeFetchResult:
    """A range aggregate with immutable outcome metadata and copied payloads."""

    start_date: str
    end_date: str
    outcomes: tuple[ArchiveDateOutcome, ...]
    collection: dict[str, object]
    products: tuple[ArchiveProduct, ...]
    documents: tuple[ArchiveDocument, ...]

    @property
    def requested_date_count(self) -> int:
        """Return the number of inclusive dates requested."""

        return len(self.outcomes)

    @property
    def successful_date_count(self) -> int:
        """Return the number of dates that loaded, including empty dates."""

        return sum(outcome.succeeded for outcome in self.outcomes)

    @property
    def failed_date_count(self) -> int:
        """Return the number of dates with a retained safe failure."""

        return self.requested_date_count - self.successful_date_count

    @property
    def feature_count(self) -> int:
        """Return the number of features in the fresh merged collection."""

        features = self.collection["features"]
        assert isinstance(features, list)
        return len(features)


def inclusive_archive_dates(
    start_date: ArchiveDateLike,
    end_date: ArchiveDateLike,
) -> tuple[str, ...]:
    """Return chronological ``YYYYMMDD`` dates for an inclusive range.

    At most the advertised rolling archive window may be requested at once.
    This bounds both network activity and the size of an aggregate.
    """

    start_text = validate_archive_date(start_date)
    end_text = validate_archive_date(end_date)
    start = datetime.strptime(start_text, "%Y%m%d").date()
    end = datetime.strptime(end_text, "%Y%m%d").date()
    if start > end:
        raise ArchiveRangeValidationError(
            "The archive range start date must not be after its end date."
        )
    count = (end - start).days + 1
    if count > MAX_ARCHIVE_RANGE_DAYS:
        raise ArchiveRangeValidationError(
            "The ECCC archive range may contain at most "
            f"{MAX_ARCHIVE_RANGE_DAYS} inclusive dates."
        )
    return tuple(
        (start + timedelta(days=offset)).strftime("%Y%m%d")
        for offset in range(count)
    )


def combine_archive_range(
    start_date: ArchiveDateLike,
    end_date: ArchiveDateLike,
    *,
    successes: Sequence[ArchiveSuccess] = (),
    failures: Sequence[ArchiveFailure] = (),
) -> ArchiveRangeFetchResult:
    """Combine independently fetched dates into one fresh range result.

    Every requested date must appear exactly once in ``successes`` or
    ``failures``.  Successful empty FeatureCollections count as successes.
    Failures accept only display-safe :class:`ArchiveError` instances or
    strings explicitly declared safe by the caller.

    Features are retained per issuance, even when their IDs repeat on another
    date.  Aggregate product and raw-document views deduplicate exact product
    URLs because a product URL identifies one immutable Datamart file.
    """

    requested = inclusive_archive_dates(start_date, end_date)
    expected = set(requested)
    indexed: dict[str, ArchiveDateOutcome] = {}

    for value, result in _validated_pairs(successes, "successes"):
        issue_date = _validate_outcome_date(value, expected, indexed)
        copied = _copy_fetch_result(result, issue_date)
        indexed[issue_date] = ArchiveDateOutcome(
            issue_date=issue_date,
            result=copied,
        )

    for value, error in _validated_pairs(failures, "failures"):
        issue_date = _validate_outcome_date(value, expected, indexed)
        error_type, message = _safe_failure(error)
        indexed[issue_date] = ArchiveDateOutcome(
            issue_date=issue_date,
            error_type=error_type,
            error_message=message,
        )

    missing = [value for value in requested if value not in indexed]
    if missing:
        raise ArchiveRangeValidationError(
            "Every requested archive date must have a success or failure "
            f"outcome; missing {', '.join(missing)}."
        )

    outcomes = tuple(indexed[value] for value in requested)
    features: list[object] = []
    product_by_url: dict[str, ArchiveProduct] = {}
    document_by_url: dict[str, ArchiveDocument] = {}

    for outcome in outcomes:
        if outcome.result is None:
            continue
        date_features = outcome.result.collection["features"]
        assert isinstance(date_features, list)
        if len(features) + len(date_features) > MAX_TOTAL_FEATURES:
            raise ECCCArchiveResponseError(
                "The selected ECCC archive date range contains more than "
                f"{MAX_TOTAL_FEATURES} features. Choose a shorter range."
            )
        features.extend(deepcopy(date_features))
        for product in outcome.result.products:
            previous = product_by_url.get(product.url)
            if previous is not None and previous != product:
                raise ArchiveRangeValidationError(
                    "One archive product URL had conflicting metadata."
                )
            product_by_url[product.url] = product
        for document in outcome.result.documents:
            previous = document_by_url.get(document.product.url)
            if previous is not None and previous != document:
                raise ArchiveRangeValidationError(
                    "One archive product URL had conflicting raw documents."
                )
            document_by_url[document.product.url] = document

    if len(product_by_url) > MAX_ARCHIVE_FILES:
        raise ECCCArchiveResponseError(
            "The selected ECCC archive date range contains more than "
            f"{MAX_ARCHIVE_FILES} products. Choose a shorter range."
        )
    if len(document_by_url) > MAX_ARCHIVE_FILES:
        raise ECCCArchiveResponseError(
            "The selected ECCC archive date range contains more than "
            f"{MAX_ARCHIVE_FILES} raw documents. Choose a shorter range."
        )

    products = tuple(
        sorted(product_by_url.values(), key=lambda item: item.url)
    )
    documents = tuple(
        sorted(
            document_by_url.values(),
            key=lambda item: item.product.url,
        )
    )
    return ArchiveRangeFetchResult(
        start_date=requested[0],
        end_date=requested[-1],
        outcomes=outcomes,
        collection={"type": "FeatureCollection", "features": features},
        products=products,
        documents=documents,
    )


def fetch_archive_range(
    client: ECCCDatamartArchiveClient,
    start_date: ArchiveDateLike,
    end_date: ArchiveDateLike,
    *,
    allow_partial: bool = True,
) -> ArchiveRangeFetchResult:
    """Fetch an inclusive range, retaining date failures when permitted.

    With ``allow_partial=False`` the first archive error is raised and no
    aggregate is returned. With the default, date-local errors are retained
    alongside successful dates. Systemic service failures and cumulative
    safety limits stop further requests and mark the remaining dates as not
    attempted.
    """

    if not isinstance(client, ECCCDatamartArchiveClient):
        raise ArchiveRangeValidationError(
            "A configured ECCC archive client is required."
        )
    if not isinstance(allow_partial, bool):
        raise ArchiveRangeValidationError(
            "The partial-range setting must be true or false."
        )
    requested = inclusive_archive_dates(start_date, end_date)
    successes: list[ArchiveSuccess] = []
    failures: list[ArchiveFailure] = []
    product_urls: set[str] = set()
    document_urls: set[str] = set()
    feature_count = 0
    for index, issue_date in enumerate(requested, start=1):
        try:
            result = client.fetch_date(issue_date)
        except ArchiveError as exc:
            if not allow_partial:
                raise
            failures.append((issue_date, exc))
            if (
                isinstance(exc, ECCCArchiveRequestError)
                and exc.systemic
            ):
                failures.extend(
                    (
                        remaining_date,
                        "Not attempted after a systemic ECCC archive "
                        "service failure.",
                    )
                    for remaining_date in requested[index:]
                )
                break
        else:
            try:
                validated = _copy_fetch_result(result, issue_date)
            except ArchiveError as exc:
                if not allow_partial:
                    raise
                failures.append((issue_date, exc))
                continue

            date_features = validated.collection["features"]
            assert isinstance(date_features, list)
            next_product_urls = product_urls | {
                product.url for product in validated.products
            }
            next_document_urls = document_urls | {
                document.product.url for document in validated.documents
            }
            next_feature_count = feature_count + len(date_features)
            limit_error: ECCCArchiveResponseError | None = None
            if (
                len(next_product_urls) > MAX_ARCHIVE_FILES
                or len(next_document_urls) > MAX_ARCHIVE_FILES
            ):
                limit_error = ECCCArchiveResponseError(
                    "The selected ECCC archive date range would exceed the "
                    f"{MAX_ARCHIVE_FILES}-file safety limit."
                )
            elif next_feature_count > MAX_TOTAL_FEATURES:
                limit_error = ECCCArchiveResponseError(
                    "The selected ECCC archive date range would exceed the "
                    f"{MAX_TOTAL_FEATURES}-feature safety limit."
                )
            if limit_error is not None:
                if not allow_partial:
                    raise limit_error
                failures.append((issue_date, limit_error))
                failures.extend(
                    (
                        remaining_date,
                        "Not attempted after the archive range reached a "
                        "safety limit.",
                    )
                    for remaining_date in requested[index:]
                )
                break

            successes.append((issue_date, validated))
            product_urls = next_product_urls
            document_urls = next_document_urls
            feature_count = next_feature_count
            reached_limit = (
                len(product_urls) == MAX_ARCHIVE_FILES
                or len(document_urls) == MAX_ARCHIVE_FILES
                or feature_count == MAX_TOTAL_FEATURES
            )
            if reached_limit and index < len(requested):
                failures.extend(
                    (
                        remaining_date,
                        "Not attempted because the archive range reached a "
                        "safety limit.",
                    )
                    for remaining_date in requested[index:]
                )
                break
    return combine_archive_range(
        requested[0],
        requested[-1],
        successes=successes,
        failures=failures,
    )


def raw_range_bundle_bytes(result: ArchiveRangeFetchResult) -> bytes:
    """Serialize per-date diagnostics and untouched raw documents as UTF-8."""

    if not isinstance(result, ArchiveRangeFetchResult):
        raise ArchiveRangeValidationError(
            "A valid archive range result is required for raw export."
        )
    dates: list[dict[str, object]] = []
    for outcome in result.outcomes:
        if outcome.result is None:
            dates.append(
                {
                    "issue_date": outcome.issue_date,
                    "status": "not_loaded",
                    "error": {
                        "type": outcome.error_type,
                        "message": outcome.error_message,
                    },
                    "files": [],
                }
            )
            continue
        files = [
            {
                "filename": document.product.filename,
                "url": document.product.url,
                "payload": json_safe(deepcopy(document.payload)),
            }
            for document in outcome.result.documents
        ]
        dates.append(
            {
                "issue_date": outcome.issue_date,
                "status": "success",
                "feature_count": outcome.feature_count,
                "product_count": outcome.product_count,
                "files": files,
            }
        )
    bundle = {
        "source": (
            "Environment and Climate Change Canada Datamart — "
            "Coastal Flooding Risk Index"
        ),
        "date_range": {
            "start": result.start_date,
            "end": result.end_date,
            "inclusive_day_count": result.requested_date_count,
        },
        "summary": {
            "successful_date_count": result.successful_date_count,
            "not_loaded_date_count": result.failed_date_count,
            "feature_count": result.feature_count,
            "product_count": len(result.products),
        },
        "dates": dates,
    }
    return json.dumps(
        bundle,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _validated_pairs(
    values: Sequence[tuple[object, object]],
    label: str,
) -> tuple[tuple[object, object], ...]:
    if isinstance(values, (str, bytes, bytearray)):
        raise ArchiveRangeValidationError(
            f"Archive range {label} must be a sequence of date-value pairs."
        )
    try:
        pairs = tuple(values)
    except TypeError as exc:
        raise ArchiveRangeValidationError(
            f"Archive range {label} must be a sequence of date-value pairs."
        ) from exc
    for pair in pairs:
        if (
            not isinstance(pair, (tuple, list))
            or len(pair) != 2
        ):
            raise ArchiveRangeValidationError(
                f"Archive range {label} must contain date-value pairs."
            )
    return pairs


def _validate_outcome_date(
    value: object,
    expected: set[str],
    indexed: dict[str, ArchiveDateOutcome],
) -> str:
    try:
        issue_date = validate_archive_date(value)  # type: ignore[arg-type]
    except (ArchiveError, TypeError, ValueError) as exc:
        raise ArchiveRangeValidationError(
            "An archive range outcome had an invalid issue date."
        ) from exc
    if issue_date not in expected:
        raise ArchiveRangeValidationError(
            f"Archive outcome {issue_date} is outside the requested range."
        )
    if issue_date in indexed:
        raise ArchiveRangeValidationError(
            f"Archive outcome {issue_date} was supplied more than once."
        )
    return issue_date


def _copy_fetch_result(
    value: object,
    issue_date: str,
) -> ArchiveFetchResult:
    if not isinstance(value, ArchiveFetchResult):
        raise ArchiveRangeValidationError(
            "Archive range successes must contain ArchiveFetchResult values."
        )
    collection = value.collection
    if (
        not isinstance(collection, dict)
        or collection.get("type") != "FeatureCollection"
        or not isinstance(collection.get("features"), list)
    ):
        raise ArchiveRangeValidationError(
            "An archive range success had an invalid FeatureCollection."
        )
    if not all(isinstance(item, ArchiveProduct) for item in value.products):
        raise ArchiveRangeValidationError(
            "An archive range success had invalid product metadata."
        )
    if not all(isinstance(item, ArchiveDocument) for item in value.documents):
        raise ArchiveRangeValidationError(
            "An archive range success had invalid raw documents."
        )
    if any(
        not product.filename.startswith(issue_date)
        for product in value.products
    ):
        raise ArchiveRangeValidationError(
            "An archive range product did not match its issue date."
        )
    if any(
        not document.product.filename.startswith(issue_date)
        for document in value.documents
    ):
        raise ArchiveRangeValidationError(
            "An archive range raw document did not match its issue date."
        )
    return ArchiveFetchResult(
        collection=deepcopy(collection),
        products=tuple(value.products),
        documents=tuple(
            ArchiveDocument(
                product=document.product,
                payload=deepcopy(document.payload),
            )
            for document in value.documents
        ),
    )


def _safe_failure(value: object) -> tuple[str, str]:
    if isinstance(value, ArchiveError):
        message = str(value).strip()
        error_type = type(value).__name__
    elif isinstance(value, str):
        message = value.strip()
        error_type = "ArchiveDateFailure"
    else:
        raise ArchiveRangeValidationError(
            "Archive range failures must contain a safe archive error or "
            "message."
        )
    if not message:
        raise ArchiveRangeValidationError(
            "Archive range failure messages must not be empty."
        )
    return error_type, message
