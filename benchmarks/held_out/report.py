"""Held-out evaluation (C07): run the generated schedules once, report as found.

Nothing here has an expected mutant verdict, because none was chosen: the
schedules came from seeds, not from the mutant's behaviour. The checks that do
have a known answer are:

* the correct variant must pass (replay / no false alarm), on both engines;
* Track A and the independent self-check must agree on whether the mutant
  fails and, if so, on the first-failure anchor (cross-engine agreement);
* any run that errors is counted and listed, never dropped.

Mutant detection and localization are *observed* counts with denominators.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from benchmarks.common.cases import CASES, Case
from benchmarks.release_evidence import _anchor, _mutated_rank
from src.app.adapters import SelfCheckComparison, TrackBCausal, WaveformComparison
from src.app.runner import Limits, Runner
from src.app.service import AnalysisService
from src.contracts import AnalysisStatus

ENGINES = ("track-a", "self-check")


@dataclass
class HeldRow:
    case: str
    schedule: str
    engine: str
    variant: str
    status: str
    anchor: str
    rank: int | str = ""  # track-a mutant FAIL only


def held_out_schedules(case: Case) -> list[str]:
    manifest = json.loads((case.directory / "manifest.json").read_text())
    return sorted(n for n, v in manifest["schedules"].items() if v.get("held_out"))


def run_held_out(out: Path) -> list[HeldRow]:
    rows: list[HeldRow] = []
    for engine in ENGINES:
        comparison = WaveformComparison() if engine == "track-a" else SelfCheckComparison()
        causal = TrackBCausal() if engine == "track-a" else None
        service = AnalysisService(Runner(out / "artifacts-held-out", Limits(wall_clock_s=180)), comparison, causal)
        for case in CASES:
            for name in held_out_schedules(case):
                for variant in ("correct", "mutant"):
                    outcome = service.analyze(case.directory / "manifest.json", variant=variant, schedule=name)
                    rank: int | str = ""
                    if variant == "mutant" and engine == "track-a" and outcome.status is AnalysisStatus.FAIL:
                        rank = _mutated_rank(case, outcome)
                    rows.append(HeldRow(case.case_id, name, engine, variant, outcome.status.value, _anchor(outcome), rank))
    return rows


def _pairs(rows: list[HeldRow]) -> dict[tuple[str, str], dict[str, HeldRow]]:
    pairs: dict[tuple[str, str], dict[str, HeldRow]] = {}
    for r in rows:
        if r.variant == "mutant":
            pairs.setdefault((r.case, r.schedule), {})[r.engine] = r
    return pairs


def summarize(rows: list[HeldRow]) -> dict[str, object]:
    correct = [r for r in rows if r.variant == "correct"]
    pairs = _pairs(rows)
    ta = [p["track-a"] for p in pairs.values() if "track-a" in p]
    detected = [r for r in ta if r.status == AnalysisStatus.FAIL.value]
    agree = [
        k for k, p in pairs.items()
        if set(p) == set(ENGINES) and p["track-a"].status == p["self-check"].status
        and (p["track-a"].status != AnalysisStatus.FAIL.value or p["track-a"].anchor == p["self-check"].anchor)
    ]
    return {
        "schedules": len(pairs),
        "correct_passes": [sum(r.status == "pass" for r in correct), len(correct)],
        "mutant_detected_track_a": [len(detected), len(ta)],
        "mutant_not_detected_track_a": [sum(r.status == "pass" for r in ta), len(ta)],
        "mutant_errors_track_a": [sum(r.status not in ("pass", "fail") for r in ta), len(ta)],
        "engines_agree": [len(agree), len(pairs)],
        "localized": {
            "ranked": [sum(isinstance(r.rank, int) for r in detected), len(detected)],
            **{f"top{k}": [sum(isinstance(r.rank, int) and r.rank <= k for r in detected), len(detected)] for k in (1, 2, 5)},
        },
    }


def problems(rows: list[HeldRow]) -> list[str]:
    """What must fail the run: false alarms, errors, and engine disagreement."""
    out = [f"{r.engine} {r.case}/{r.schedule}: correct variant {r.status}" for r in rows
           if r.variant == "correct" and r.status != "pass"]
    out += [f"{r.engine} {r.case}/{r.schedule}: mutant run {r.status}" for r in rows
            if r.variant == "mutant" and r.status not in ("pass", "fail")]
    for (case, sched), p in _pairs(rows).items():
        a, b = p.get("track-a"), p.get("self-check")
        if a and b and (a.status != b.status or (a.status == "fail" and a.anchor != b.anchor)):
            out.append(f"engines disagree on {case}/{sched}: track-a {a.status} {a.anchor!r} vs self-check {b.status} {b.anchor!r}")
    return out


def markdown(rows: list[HeldRow]) -> str:
    s = summarize(rows)
    n = s["schedules"]
    cases = len({r.case for r in rows})
    c, d, nd, er = s["correct_passes"], s["mutant_detected_track_a"], s["mutant_not_detected_track_a"], s["mutant_errors_track_a"]  # type: ignore[misc]
    loc: dict[str, list[int]] = s["localized"]  # type: ignore[assignment]
    lines = [
        "", "## Held-out schedules (seeded, generated once, reported as found)", "",
        f"{n} schedules over {cases} cases, produced by `benchmarks/held_out/generate.py` from fixed seeds. "
        "No mutant verdict was chosen in advance, so detection below is observed, not checked against an "
        "expectation. The schedules were written by the same session that built the tools: held out from tuning, "
        "not from authorship. They reuse the same four designs and mutations.", "",
        "| Check | Count | Total |", "| --- | ---: | ---: |",
        f"| correct variant passes (both engines) | {c[0]} | {c[1]} |",
        f"| mutant detected (Track A) | {d[0]} | {d[1]} |",
        f"| mutant not detected (passes) | {nd[0]} | {nd[1]} |",
        f"| mutant run errored | {er[0]} | {er[1]} |",
        f"| Track A and self-check agree (status and first anchor) | {s['engines_agree'][0]} | {n} |",  # type: ignore[index]
        "", f"Localization (Track B), over the {d[0]} detected mutants only; undetected mutants have nothing to localize:", "",
        f"- ranked at all: {loc['ranked'][0]}/{loc['ranked'][1]}",
        *(f"- top-{k}: {loc[f'top{k}'][0]}/{loc[f'top{k}'][1]}" for k in (1, 2, 5)), "",
        "| Case | Schedule | Track A | Self-check | Rank |", "| --- | --- | --- | --- | ---: |",
    ]
    for (case, sched), p in sorted(_pairs(rows).items()):
        a, b = p.get("track-a"), p.get("self-check")
        lines.append(f"| {case} | {sched} | {a.status if a else '-'} | {b.status if b else '-'} | {a.rank if a else ''} |")
    bad = problems(rows)
    lines += ["", "### Held-out problems", ""] + ([f"- {p}" for p in bad] or ["None."]) + [""]
    return "\n".join(lines)
