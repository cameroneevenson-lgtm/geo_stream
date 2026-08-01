"""Pure CaSR-Rivers selection helpers and fetch orchestration.

Keeps decision logic out of ``app.py``. Network work is injected via callables
so Streamlit can wrap downloads in ``st.cache_data`` with primitive keys.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .casr_common import (
    CASRBasinHit,
    CASRConfigurationError,
    CASRDataUnavailableError,
    CASRRiversFile,
    PROBE_VARIABLE,
    normalize_variable,
    parse_month_token,
)
from .casr_processing import (
    CASRSubset,
    grid_bbox_from_bytes,
    process_rivers_bytes,
    subset_intersects_roi,
)
from .geometry import GeometryError, parse_roi, roi_bbox

MAX_PROBE_DOWNLOADS = 24
MAX_BASINS = 3


@dataclass(frozen=True, slots=True)
class CASRFetchResult:
    """One or more ROI-intersecting CaSR-Rivers subsets for the map."""

    year_month: str
    variable: str
    subsets: tuple[CASRSubset, ...]
    probed: int
    warnings: tuple[str, ...] = ()


ListFiles = Callable[[str, str | None], tuple[CASRRiversFile, ...]]
Download = Callable[[CASRRiversFile], bytes]


def files_for_variable(
    files: Sequence[CASRRiversFile],
    variable: str,
) -> tuple[CASRRiversFile, ...]:
    """Filter discovered files to one product variable."""

    wanted = normalize_variable(variable)
    if wanted is None:
        raise CASRConfigurationError(
            "CaSR-Rivers variable must be RiverDischarge, "
            "RiverChannelStorage, or DeepReservoirStorage."
        )
    return tuple(file for file in files if file.variable == wanted)


def unique_subbasin_ids(files: Sequence[CASRRiversFile]) -> tuple[str, ...]:
    """Return sorted unique sub-basin IDs from a file listing."""

    return tuple(sorted({file.subbasin_id for file in files}))


def prioritize_probe_files(
    probe_files: Sequence[CASRRiversFile],
    *,
    roi_bbox_wgs84: tuple[float, float, float, float],
    limit: int = MAX_PROBE_DOWNLOADS,
) -> tuple[CASRRiversFile, ...]:
    """Order probe files by a coarse drainage-prefix heuristic near the ROI.

    NHN-style IDs are not a full spatial index. Prefer prefixes that historically
    cover eastern Canada when the ROI is Atlantic-facing, otherwise keep lexical
    order. Exact intersection is still decided after each probe download.
    """

    if limit < 1:
        raise CASRConfigurationError("Probe limit must be at least 1.")
    preferred = _preferred_prefixes(roi_bbox_wgs84)
    ranked = sorted(
        probe_files,
        key=lambda file: (
            0 if any(file.subbasin_id.startswith(p) for p in preferred) else 1,
            file.subbasin_id,
        ),
    )
    # One probe file per sub-basin.
    seen: set[str] = set()
    selected: list[CASRRiversFile] = []
    for file in ranked:
        if file.subbasin_id in seen:
            continue
        seen.add(file.subbasin_id)
        selected.append(file)
        if len(selected) >= limit:
            break
    return tuple(selected)


def find_basins_for_roi(
    *,
    year_month: str,
    roi: Any,
    list_files: ListFiles,
    download: Download,
    max_probe: int = MAX_PROBE_DOWNLOADS,
    max_basins: int = MAX_BASINS,
) -> tuple[CASRBasinHit, ...]:
    """Probe cheap DeepReservoirStorage files until ROI-intersecting basins found."""

    month = parse_month_token(year_month)
    try:
        geometry = parse_roi(roi)
        bbox = roi_bbox(geometry)
    except GeometryError as exc:
        raise CASRConfigurationError(str(exc)) from exc

    probe_files = files_for_variable(
        list_files(month, PROBE_VARIABLE),
        PROBE_VARIABLE,
    )
    candidates = prioritize_probe_files(
        probe_files,
        roi_bbox_wgs84=bbox,
        limit=max_probe,
    )
    hits: list[CASRBasinHit] = []
    probed = 0
    for file in candidates:
        data = download(file)
        probed += 1
        try:
            if not subset_intersects_roi(data, roi):
                continue
            hits.append(
                CASRBasinHit(
                    subbasin_id=file.subbasin_id,
                    bbox=grid_bbox_from_bytes(data),
                    probe_url=file.url,
                )
            )
        except CASRDataUnavailableError:
            continue
        if len(hits) >= max_basins:
            break
    if not hits:
        raise CASRDataUnavailableError(
            "No CaSR-Rivers sub-basins intersecting the drawn region were "
            f"found after probing {probed} basin grids for {month}. "
            "Try a larger ROI, another month, or an explicit sub-basin ID."
        )
    return tuple(hits)


def fetch_for_roi(
    *,
    year_month: str,
    variable: str,
    roi: Any,
    list_files: ListFiles,
    download: Download,
    subbasin_id: str | None = None,
    valid_time: datetime | None = None,
    max_probe: int = MAX_PROBE_DOWNLOADS,
    max_basins: int = MAX_BASINS,
) -> CASRFetchResult:
    """Fetch and process CaSR-Rivers subsets for the ROI (or one explicit basin)."""

    month = parse_month_token(year_month)
    wanted = normalize_variable(variable)
    if wanted is None:
        raise CASRConfigurationError(
            "CaSR-Rivers variable must be RiverDischarge, "
            "RiverChannelStorage, or DeepReservoirStorage."
        )

    warnings: list[str] = []
    probed = 0
    if subbasin_id and isinstance(subbasin_id, str) and subbasin_id.strip():
        basin_ids = (subbasin_id.strip(),)
    else:
        hits = find_basins_for_roi(
            year_month=month,
            roi=roi,
            list_files=list_files,
            download=download,
            max_probe=max_probe,
            max_basins=max_basins,
        )
        # find_basins downloads until max_basins hits; approximate probed as
        # hit count when the caller only needs a progress hint.
        probed = len(hits)
        basin_ids = tuple(hit.subbasin_id for hit in hits)
        if len(hits) >= max_basins:
            warnings.append(
                f"Showing the first {max_basins} intersecting CaSR-Rivers "
                "sub-basins from the probe set. Draw a smaller ROI to narrow."
            )

    product_files = {
        file.subbasin_id: file
        for file in files_for_variable(list_files(month, wanted), wanted)
    }
    subsets: list[CASRSubset] = []
    for basin_id in basin_ids:
        file = product_files.get(basin_id)
        if file is None:
            warnings.append(
                f"No {wanted} file was listed for sub-basin {basin_id}."
            )
            continue
        data = download(file)
        subsets.append(
            process_rivers_bytes(
                data,
                roi=roi,
                variable=wanted,
                subbasin_id=basin_id,
                valid_time=valid_time,
            )
        )
    if not subsets:
        raise CASRDataUnavailableError(
            "CaSR-Rivers data was listed but no usable subset could be built "
            "for the drawn region."
        )
    return CASRFetchResult(
        year_month=month,
        variable=wanted,
        subsets=tuple(subsets),
        probed=probed,
        warnings=tuple(warnings),
    )


def _preferred_prefixes(
    bbox: tuple[float, float, float, float],
) -> tuple[str, ...]:
    """Return coarse NHN work-unit prefixes to try first for a WGS84 bbox."""

    min_lon, min_lat, max_lon, max_lat = bbox
    center_lon = (min_lon + max_lon) / 2.0
    center_lat = (min_lat + max_lat) / 2.0
    # Atlantic / Maritimes / Gulf of St. Lawrence
    if center_lon >= -70.0 and center_lat <= 52.0:
        return ("01", "02", "01A", "01B")
    # St. Lawrence / Great Lakes corridor
    if -90.0 <= center_lon < -70.0:
        return ("02", "04", "05", "02O")
    # Prairies / central
    if -110.0 <= center_lon < -90.0:
        return ("05", "07", "06")
    # Pacific / west
    if center_lon < -110.0:
        return ("08", "10", "09", "11")
    return ("01", "02", "05", "08")
