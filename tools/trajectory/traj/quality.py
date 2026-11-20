"""The data-quality gate: `build` exits 1 on these, findings never do."""

from __future__ import annotations

from traj.limits import QUALITY_MALFORMED_MAX_PERCENT, QUALITY_UNKNOWN_MAX_PERCENT
from traj.report import Coverage, coverage_problem


def check_quality(cov: Coverage) -> list[str]:
    """Data-quality error reasons (empty when none). Integer math: no float compare at the edge."""
    assert coverage_problem(cov) is None, "quality is only judged on sound coverage"
    reasons: list[str] = []
    unknown_format = dict(cov.skipped)["unknown format"]
    if unknown_format:
        reasons.append(f"{unknown_format} sessions skipped as unknown format")
    if cov.malformed_events * 100 > cov.events_used * QUALITY_MALFORMED_MAX_PERCENT:
        reasons.append(
            f"malformed events {cov.malformed_events} of {cov.events_used} used events "
            f"is over {QUALITY_MALFORMED_MAX_PERCENT} percent"
        )
    if cov.unknown_events * 100 > cov.events_total * QUALITY_UNKNOWN_MAX_PERCENT:
        reasons.append(
            f"unknown events {cov.unknown_events} of {cov.events_total} events "
            f"is over {QUALITY_UNKNOWN_MAX_PERCENT} percent"
        )
    assert all(reasons), "a reason is never blank"
    return reasons
