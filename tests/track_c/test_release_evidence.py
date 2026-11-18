"""The C07 evidence generator must agree with the documented verdict table and
must fail loudly (non-zero) when any verdict is contradicted."""

from __future__ import annotations

from pathlib import Path

import pytest

from benchmarks import release_evidence as ev
from src.contracts import AnalysisStatus
from benchmarks.common.cases import CASES, Case


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


def _row(engine: str, rank: int | str, schedule: str = "directed") -> ev.Row:
    return ev.Row(CASES[0].case_id, schedule, "mutant", engine, "fail", "fail", True, "", rank)


def test_localization_denominator_counts_only_localizing_engines_and_keeps_errors() -> None:
    case = CASES[0]
    detected = next(s for s in case.schedules if s.mutant_detected)
    masked = next((s for s in case.schedules if not s.mutant_detected), None)

    class Out:
        def __init__(self, status: AnalysisStatus) -> None:
            self.status = status
            self.causal = None

    assert ev._localization(case, detected, "mutant", "self-check", Out(AnalysisStatus.FAIL)) == ""
    assert ev._localization(case, detected, "mutant", "track-a", Out(AnalysisStatus.ERROR)) == "unranked"
    assert ev._localization(case, detected, "mutant", "track-a", Out(AnalysisStatus.PASS)) == "unranked"
    assert ev._localization(case, detected, "correct", "track-a", Out(AnalysisStatus.PASS)) == ""
    if masked is not None:
        assert ev._localization(case, masked, "mutant", "track-a", Out(AnalysisStatus.PASS)) == ""


def test_report_does_not_double_count_or_overclaim() -> None:
    rows = [_row("track-a", 1), _row("track-a", "unranked", "full_hold")]
    text = ev.markdown(rows, ev.summarize(rows), {"platform": "x", "python": "y", "tools": {}})
    assert "ranked at all: 1/2" in text and "top-1: 1/2" in text
    assert "about 1 independent observations" in text and "no held-out set" in text


def test_stale_grader_is_refused_not_ranked(tmp_path: Path) -> None:
    """A report citing a different revision than the grader's must not be ranked."""
    import hashlib
    import json
    from types import SimpleNamespace

    case = CASES[0]
    truth = json.loads((case.directory.parent / "graders" / f"{case.case_id}.json").read_text())
    span = truth["mutation"]["accepted_spans"][0]
    rel = Path(span["path"]).relative_to(Path("benchmarks") / case.directory.name)
    real = hashlib.sha256((case.directory / rel).read_bytes()).hexdigest()
    assert real == span["sha256"]  # the committed grader is current

    def outcome(cited_sha: str) -> SimpleNamespace:
        src = SimpleNamespace(id="s0", path=str(rel), sha256=cited_sha)
        loc = SimpleNamespace(source_id="s0", line=span["start_line"], end_line=span["end_line"])
        sus = SimpleNamespace(rank=1, location=loc)
        return SimpleNamespace(causal=SimpleNamespace(sources=[src], suspects=[sus]))

    assert ev._mutated_rank(case, outcome(real)) == 1
    with pytest.raises(ev.StaleGraderError):
        ev._mutated_rank(case, outcome("0" * 64))
