"""The C07 evidence generator must agree with the documented verdict table and
must fail loudly (non-zero) when any verdict is contradicted."""

from __future__ import annotations

from pathlib import Path

import pytest

from benchmarks import release_evidence as ev
from benchmarks.common.cases import CASES


def test_expected_verdicts_follow_the_registry() -> None:
    case = CASES[0]
    detected = next(s for s in case.schedules if s.mutant_detected)
    masked = next(s for s in case.schedules if not s.mutant_detected)
    assert ev._expected(case, detected, "mutant") == "fail"
    assert ev._expected(case, masked, "mutant") == "pass"
    assert ev._expected(case, detected, "correct") == "pass"


def test_summary_reports_denominators_and_flags_contradictions() -> None:
    case = CASES[0]
    sched = next(s for s in case.schedules if s.mutant_detected)
    good = ev.Row(case.case_id, sched.name, "mutant", "track-a", "fail", "fail", True, "")
    bad = ev.Row(case.case_id, sched.name, "correct", "track-a", "fail", "pass", False, "")
    summary = ev.summarize([good, bad])["track-a"]
    assert summary["all runs matching documented verdict"] == (1, 2)  # type: ignore[index]
    assert "Contradicted verdicts" in ev.markdown([good, bad], ev.summarize([good, bad]), {"platform": "x", "python": "y", "tools": {}})


@pytest.mark.integration
def test_full_matrix_matches_documented_verdicts(tmp_path: Path) -> None:
    assert ev.main(["--out", str(tmp_path), "--engines", "track-a"]) == 0
    assert "None." in (tmp_path / "evidence.md").read_text()
