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


@pytest.mark.parametrize("engines", ["", "track-aa", "track-a,", "track-a,track-a", "self-check,unknown"])
def test_invalid_engine_selection_fails_before_tools_or_artifacts(
    engines: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object) -> None:
        pytest.fail("invalid engine selection must not probe tools or run analysis")
    monkeypatch.setattr(ev, "_tool_version", forbidden)
    monkeypatch.setattr(ev, "run", forbidden)
    out = tmp_path / "not-created"
    with pytest.raises(SystemExit) as exc:
        ev.main(["--engines", engines, "--out", str(out)])
    assert exc.value.code == 2
    assert not out.exists()


@pytest.mark.parametrize("engines", [(), ("bogus",), ("track-a", "track-a")])
def test_programmatic_run_also_rejects_invalid_engines(engines: tuple[str, ...], tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        ev.run(tmp_path, engines)


def test_engine_selection_preserves_explicit_order() -> None:
    assert ev.parse_engines(" self-check, track-a ") == ("self-check", "track-a")
