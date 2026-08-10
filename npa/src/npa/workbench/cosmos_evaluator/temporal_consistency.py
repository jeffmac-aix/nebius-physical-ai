"""Source-relative temporal consistency for input-conditioned video variants.

This is an NPA companion check, not an upstream NVIDIA Cosmos Evaluator check.
It compares temporal acceleration in the source and augmented clips and fails on
excess frame-to-frame surface variation.  The comparison is source-relative so
camera and object motion already present in the input remains valid.

The default regions are the full frame and a 2x2 grid.  Taking the lowest region
score prevents a localized artifact from being hidden by a clean frame average.
Callers may instead provide normalized rectangular regions as JSON.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from npa.workbench.cosmos_evaluator.hallucination import _iter_gray_frames, _probe_size
from npa.workbench.cosmos_evaluator.upstream import CosmosEvaluatorError

DEFAULT_THRESHOLD = 0.8
MIN_REFERENCE_ACCELERATION = 1.0
ENGINE = "npa-source-relative-temporal-consistency-v1"

DEFAULT_REGIONS: tuple[tuple[str, tuple[float, float, float, float]], ...] = (
    ("full-frame", (0.0, 0.0, 1.0, 1.0)),
    ("tile-0", (0.0, 0.0, 0.5, 0.5)),
    ("tile-1", (0.5, 0.0, 1.0, 0.5)),
    ("tile-2", (0.0, 0.5, 0.5, 1.0)),
    ("tile-3", (0.5, 0.5, 1.0, 1.0)),
)


@dataclass(frozen=True)
class TemporalRegionResult:
    region_id: str
    bounds: tuple[float, float, float, float]
    source_mean_acceleration: float
    augmented_mean_acceleration: float
    acceleration_ratio: float
    score: float
    passed: bool


@dataclass(frozen=True)
class TemporalConsistencyResult:
    clip_id: str
    passed: bool
    threshold: float
    score: float
    total_frames: int
    frame_counts_match: bool
    engine: str = ENGINE
    aggregation: str = "minimum-region-score"
    regions: list[TemporalRegionResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_regions(
    value: str | Sequence[Any] | None,
) -> list[tuple[str, tuple[float, float, float, float]]]:
    """Parse normalized rectangles, or return the generic tiled default."""

    if value is None or value == "" or (
        isinstance(value, (list, tuple)) and not value
    ):
        return list(DEFAULT_REGIONS)
    raw: Any = value
    if isinstance(value, str):
        try:
            raw = json.loads(value)
        except json.JSONDecodeError as exc:
            raise CosmosEvaluatorError("--temporal-regions-json must be valid JSON") from exc
    if not isinstance(raw, list) or not raw:
        raise CosmosEvaluatorError("--temporal-regions-json must be a non-empty JSON list")

    parsed: list[tuple[str, tuple[float, float, float, float]]] = []
    for index, item in enumerate(raw):
        region_id = f"region-{index}"
        bounds: Any = item
        if isinstance(item, dict):
            region_id = str(item.get("id") or region_id)
            bounds = item.get("bounds")
        if not isinstance(bounds, (list, tuple)) or len(bounds) != 4:
            raise CosmosEvaluatorError(f"temporal region {index} must have four normalized bounds")
        try:
            x0, y0, x1, y1 = (float(part) for part in bounds)
        except (TypeError, ValueError) as exc:
            raise CosmosEvaluatorError(f"temporal region {index} has non-numeric bounds") from exc
        if not (0.0 <= x0 < x1 <= 1.0 and 0.0 <= y0 < y1 <= 1.0):
            raise CosmosEvaluatorError(
                f"temporal region {index} bounds must satisfy 0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1"
            )
        parsed.append((region_id, (x0, y0, x1, y1)))
    return parsed


def check_temporal_consistency(
    *,
    clip_id: str,
    original_video: str | Path,
    augmented_video: str | Path,
    threshold: float = DEFAULT_THRESHOLD,
    regions: str | Sequence[Any] | None = None,
) -> TemporalConsistencyResult:
    """Compare source and augmented temporal acceleration over normalized regions."""

    original = Path(original_video)
    augmented = Path(augmented_video)
    for label, path in (("original", original), ("augmented", augmented)):
        if not path.is_file():
            raise CosmosEvaluatorError(f"{label} video not found: {path}")
    if not 0.0 < threshold <= 1.0:
        raise CosmosEvaluatorError("temporal threshold must be greater than 0.0 and at most 1.0")

    normalized_regions = parse_regions(regions)
    height, width = _probe_size(original)
    source_frames = _iter_gray_frames(original, height, width)
    augmented_frames = _iter_gray_frames(augmented, height, width)
    source_sums = np.zeros(len(normalized_regions), dtype=np.float64)
    augmented_sums = np.zeros(len(normalized_regions), dtype=np.float64)
    acceleration_frames = 0
    total_frames = 0
    counts_match = True

    try:
        source_window = [next(source_frames, None), next(source_frames, None)]
        augmented_window = [next(augmented_frames, None), next(augmented_frames, None)]
        if any(frame is None for frame in source_window + augmented_window):
            raise CosmosEvaluatorError("temporal consistency needs at least three decodable frames per clip")
        total_frames = 2
        while True:
            source_current = next(source_frames, None)
            augmented_current = next(augmented_frames, None)
            if source_current is None or augmented_current is None:
                counts_match = source_current is None and augmented_current is None
                break
            source_acceleration = np.abs(
                source_current.astype(np.float32)
                - 2.0 * source_window[1].astype(np.float32)
                + source_window[0].astype(np.float32)
            )
            augmented_acceleration = np.abs(
                augmented_current.astype(np.float32)
                - 2.0 * augmented_window[1].astype(np.float32)
                + augmented_window[0].astype(np.float32)
            )
            for index, (_, bounds) in enumerate(normalized_regions):
                y_slice, x_slice = _region_slices(bounds, height=height, width=width)
                source_sums[index] += float(source_acceleration[y_slice, x_slice].mean())
                augmented_sums[index] += float(augmented_acceleration[y_slice, x_slice].mean())
            source_window = [source_window[1], source_current]
            augmented_window = [augmented_window[1], augmented_current]
            acceleration_frames += 1
            total_frames += 1
    finally:
        source_frames.close()
        augmented_frames.close()

    if acceleration_frames == 0:
        raise CosmosEvaluatorError("temporal consistency needs at least three decodable frames per clip")

    results: list[TemporalRegionResult] = []
    for index, (region_id, bounds) in enumerate(normalized_regions):
        source_mean = float(source_sums[index] / acceleration_frames)
        augmented_mean = float(augmented_sums[index] / acceleration_frames)
        reference = max(source_mean, MIN_REFERENCE_ACCELERATION)
        ratio = augmented_mean / reference
        score = min(1.0, reference / max(augmented_mean, reference))
        results.append(
            TemporalRegionResult(
                region_id=region_id,
                bounds=bounds,
                source_mean_acceleration=round(source_mean, 6),
                augmented_mean_acceleration=round(augmented_mean, 6),
                acceleration_ratio=round(ratio, 6),
                score=round(score, 6),
                passed=score >= threshold,
            )
        )

    score = min(region.score for region in results)
    passed = counts_match and total_frames >= 3 and all(region.passed for region in results)
    return TemporalConsistencyResult(
        clip_id=clip_id,
        passed=passed,
        threshold=threshold,
        score=score,
        total_frames=total_frames,
        frame_counts_match=counts_match,
        regions=results,
    )


def _region_slices(
    bounds: tuple[float, float, float, float], *, height: int, width: int
) -> tuple[slice, slice]:
    x0, y0, x1, y1 = bounds
    left = min(width - 1, int(x0 * width))
    top = min(height - 1, int(y0 * height))
    right = max(left + 1, min(width, int(np.ceil(x1 * width))))
    bottom = max(top + 1, min(height, int(np.ceil(y1 * height))))
    return slice(top, bottom), slice(left, right)
