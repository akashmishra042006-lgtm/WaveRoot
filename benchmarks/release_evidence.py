"""C07 release evidence: run every benchmark case x schedule x variant through
the real analysis path and compare with the documented verdicts.

Reports counts with denominators, never a rate without one. Detection, false
positives, masking controls, replay and localization are separate sections.
Localization is ``unranked`` unless a real causal adapter is supplied; fixture
causal data is never used. Exit status is non-zero if any documented verdict is
contradicted or any run is not a clean pass/fail.

    uv run python -m benchmarks.release_evidence --out /tmp/evidence
"""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from benchmarks.common.cases import CASES, Case, Schedule
from src.app.adapters import SelfCheckComparison, WaveformComparison
from src.app.runner import Limits, Runner
from src.app.service import AnalysisService
from src.contracts import AnalysisStatus, ComparisonMode

TOOLS = {"verilator": ["verilator", "--version"], "icarus": ["iverilog", "-V"]}


@dataclass
class Row:
    case: str
    schedule: str
    variant: str
    engine: str
    status: str
    expected: str
    ok: bool
    anchor: str


def _tool_version(sim: str) -> str:
    if shutil.which(TOOLS[sim][0]) is None:
        return "MISSING"
    out = subprocess.run(TOOLS[sim], capture_output=True, text=True, check=False).stdout
    return out.splitlines()[0].strip() if out else "unknown"


def _anchor(outcome) -> str:  # type: ignore[no-untyped-def]
    primary = outcome.primary
    if primary is None or primary.failure is None:
        return ""
    f = primary.failure
    o = f.observation
    return f"{f.mode.value} {f.signal_path} epoch {o.epoch} cycle {o.cycle} {o.phase.value} @{o.time_ps}ps"


def validate_engines(engines: tuple[str, ...]) -> tuple[str, ...]:
    if not engines or any(engine not in ("track-a", "self-check") for engine in engines):
        raise ValueError("engines must be a non-empty selection of track-a,self-check")
    if len(set(engines)) != len(engines):
        raise ValueError("duplicate engines would double-count release evidence")
    return engines


def parse_engines(value: str) -> tuple[str, ...]:
    try:
        return validate_engines(tuple(part.strip() for part in value.split(",")))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def run(out: Path, engines: tuple[str, ...]) -> tuple[list[Row], dict[str, object]]:
    validate_engines(engines)
    rows: list[Row] = []
    for engine in engines:
        comparison = WaveformComparison() if engine == "track-a" else SelfCheckComparison()
        service = AnalysisService(Runner(out / "artifacts", Limits(wall_clock_s=180)), comparison)
        for case in CASES:
            for sched in case.schedules:
                for variant in ("correct", "mutant"):
                    outcome = service.analyze(
                        case.directory / "manifest.json", variant=variant, schedule=sched.name
                    )
                    want = _expected(case, sched, variant)
                    rows.append(
                        Row(case.case_id, sched.name, variant, engine, outcome.status.value, want,
                            outcome.status.value == want, _anchor(outcome))
                    )
    versions = {sim: _tool_version(sim) for sim in TOOLS}
    meta = {"platform": platform.platform(), "python": sys.version.split()[0], "tools": versions}
    return rows, meta


def _expected(case: Case, sched: Schedule, variant: str) -> str:
    if variant == "correct" or not sched.mutant_detected:
        return AnalysisStatus.PASS.value
    return AnalysisStatus.FAIL.value


def summarize(rows: list[Row]) -> dict[str, object]:
    def count(pred) -> tuple[int, int]:  # type: ignore[no-untyped-def]
        sel = [r for r in rows if pred(r)]
        return sum(r.ok for r in sel), len(sel)

    summary: dict[str, object] = {}
    for engine in sorted({r.engine for r in rows}):
        e = lambda r, engine=engine: r.engine == engine  # noqa: E731
        det_expected = {(c.case_id, s.name) for c in CASES for s in c.schedules if s.mutant_detected}
        mask_expected = {(c.case_id, s.name) for c in CASES for s in c.schedules if not s.mutant_detected}
        summary[engine] = {
            "detection (mutant fails on documented-detecting schedules)": count(
                lambda r: e(r) and r.variant == "mutant" and (r.case, r.schedule) in det_expected
            ),
            "masking controls (mutant passes, documented)": count(
                lambda r: e(r) and r.variant == "mutant" and (r.case, r.schedule) in mask_expected
            ),
            "no false positives / replay (correct variant passes same inputs)": count(
                lambda r: e(r) and r.variant == "correct"
            ),
            "all runs matching documented verdict": count(e),
        }
    return summary


def markdown(rows: list[Row], summary: dict[str, object], meta: dict[str, object]) -> str:
    lines = ["# Track C release evidence", "", f"Platform: {meta['platform']}; Python {meta['python']}", ""]
    for sim, ver in dict(meta["tools"]).items():  # type: ignore[call-overload]
        lines.append(f"- {sim}: {ver}")
    lines += ["", "Localization: **unranked** (no real causal adapter ran; fixture causal data is not evidence).", ""]
    for engine, items in summary.items():
        lines += [f"## {engine}", "", "| Check | Passed | Total |", "| --- | ---: | ---: |"]
        for name, (ok, total) in items.items():  # type: ignore[attr-defined]
            lines.append(f"| {name} | {ok} | {total} |")
        lines.append("")
    bad = [r for r in rows if not r.ok]
    lines += ["## Contradicted verdicts", ""]
    lines += [f"- {r.engine} {r.case}/{r.schedule}/{r.variant}: got {r.status}, expected {r.expected}" for r in bad] or ["None."]
    lines += ["", "## Detected anchors (mutant, first failure)", "", "| Case | Schedule | Engine | Anchor |", "| --- | --- | --- | --- |"]
    lines += [f"| {r.case} | {r.schedule} | {r.engine} | {r.anchor} |" for r in rows if r.variant == "mutant" and r.anchor]
    lines += ["", "Four cases are a smoke suite, not an accuracy estimate.", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--engines", type=parse_engines, default="track-a,self-check")
    args = ap.parse_args(argv)
    out = (args.out or Path(tempfile.mkdtemp(prefix="waveroot-evidence-"))).resolve()
    out.mkdir(parents=True, exist_ok=True)
    missing = [s for s, v in ((s, _tool_version(s)) for s in TOOLS) if v == "MISSING"]
    if missing:
        print(f"required simulator missing: {', '.join(missing)} (hard gate, not a skip)", file=sys.stderr)
        return 2
    rows, meta = run(out, args.engines)
    summary = summarize(rows)
    (out / "evidence.json").write_text(json.dumps({"meta": meta, "summary": summary, "rows": [r.__dict__ for r in rows]}, indent=2))
    text = markdown(rows, summary, meta)
    (out / "evidence.md").write_text(text)
    print(text)
    return 0 if all(r.ok for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
