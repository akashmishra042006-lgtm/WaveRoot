"""Held-out schedules (C07): generation is deterministic and valid, and the
report flags false alarms, errors and engine disagreement instead of hiding them."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from benchmarks.common import benchlib
from benchmarks.common.cases import CASES
from benchmarks.held_out import generate, report
from benchmarks.held_out.report import HeldRow


def test_committed_files_match_the_generator() -> None:
    """Regenerating must reproduce the committed stim files and manifests exactly."""
    assert generate.main(["--check"]) == 0


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.case_id)
def test_every_held_out_schedule_is_registered_valid_and_distinct(case) -> None:  # type: ignore[no-untyped-def]
    manifest = json.loads((case.directory / "manifest.json").read_text())
    names = report.held_out_schedules(case)
    assert names == [generate.schedule_name(i) for i in range(1, len(generate.PROFILES[case.case_id]) + 1)]
    authored = {s.name for s in case.schedules}
    assert not authored & set(names)
    texts = set()
    for name in names:
        rel = manifest["schedules"][name]["stim"]
        assert rel in manifest["analysis_inputs"]["stimulus"]
        path = case.directory / rel
        rows = benchlib.parse_stim(path, tuple(case.golden.COLUMNS))
        assert len(rows) == generate.LENGTH + generate.DRAIN <= benchlib.CYCLE_LIMIT
        case.golden.goldens_for(path)  # the reference model accepts it
        texts.add("\n".join(p for p in path.read_text().splitlines() if not p.startswith("#")))
    assert len(texts) == len(names)  # no two schedules are the same stimulus


def test_generator_has_no_simulator_or_analysis_inputs() -> None:
    """Schedules may depend only on seeds, profiles and the reference model."""
    import ast

    tree = ast.parse(Path(generate.__file__).read_text())
    imported = {
        (n.module or "") if isinstance(n, ast.ImportFrom) else a.name
        for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in (n.names if isinstance(n, ast.Import) else [n])
    }
    assert not any(m.startswith(("subprocess", "src.app", "src.waveform", "src.causal")) for m in imported)


def _row(engine: str, variant: str, status: str, anchor: str = "", rank: int | str = "", sched: str = "held_out_01") -> HeldRow:
    return HeldRow("case_01_fifo_wrap", sched, engine, variant, status, anchor, rank)


def test_report_counts_denominators_and_flags_problems() -> None:
    good = [
        _row("track-a", "correct", "pass"), _row("self-check", "correct", "pass"),
        _row("track-a", "mutant", "fail", "A", 1), _row("self-check", "mutant", "fail", "A"),
        _row("track-a", "mutant", "pass", sched="held_out_02"), _row("self-check", "mutant", "pass", sched="held_out_02"),
    ]
    s = report.summarize(good)
    assert s["mutant_detected_track_a"] == [1, 2] and s["engines_agree"] == [2, 2]
    assert s["localized"]["top1"] == [1, 1]  # type: ignore[index]
    assert report.problems(good) == []
    text = report.markdown(good)
    assert "1 | 2 |" in text and "held out from tuning, not from authorship" in text

    bad = [*good,
           _row("track-a", "correct", "fail", sched="held_out_03"),
           _row("track-a", "mutant", "error", sched="held_out_04"),
           _row("track-a", "mutant", "fail", "A", 2, "held_out_05"), _row("self-check", "mutant", "fail", "B", sched="held_out_05")]
    found = report.problems(bad)
    assert any("correct variant fail" in p for p in found)
    assert any("mutant run error" in p for p in found)
    assert any("disagree" in p and "held_out_05" in p for p in found)
    assert report.summarize(bad)["mutant_errors_track_a"][0] == 1  # type: ignore[index]


@pytest.mark.integration
def test_one_real_held_out_schedule_runs_on_both_engines(tmp_path: Path) -> None:
    case = next(c for c in CASES if c.case_id == "case_02_axi_skid")
    if shutil.which("verilator") is None:
        pytest.fail("verilator required (hard gate, not a skip)")
    from src.app.adapters import SelfCheckComparison, WaveformComparison
    from src.app.runner import Limits, Runner
    from src.app.service import AnalysisService

    results = {}
    for engine, comparison in (("track-a", WaveformComparison()), ("self-check", SelfCheckComparison())):
        service = AnalysisService(Runner(tmp_path / engine, Limits(wall_clock_s=180)), comparison)
        for variant in ("correct", "mutant"):
            results[engine, variant] = service.analyze(case.directory / "manifest.json", variant=variant, schedule="held_out_01")
    assert results["track-a", "correct"].status.value == results["self-check", "correct"].status.value == "pass"
    a, b = results["track-a", "mutant"], results["self-check", "mutant"]
    assert a.status == b.status
    if a.status.value == "fail":
        assert report._anchor(a) == report._anchor(b)
