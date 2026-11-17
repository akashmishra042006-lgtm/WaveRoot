"""WaveRoot Track C command line.

    python -m src.app.cli analyze MANIFEST --variant mutant --schedule directed
    python -m src.app.cli open REPORT_JSON

Exit codes follow spec.md section 8.4: 0 pass, 1 fail, 2 invalid_input or
error, 3 unsupported or inconclusive. Ctrl-C cancels a running job and reaps
the simulator's process group.

Waveform options from Track A are wired here once Track A's provider is on
``main``; until then comparison uses the labeled benchmark self-check.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
from collections.abc import Sequence
from pathlib import Path
from types import FrameType

from src.app.adapters import FixtureCausal, SelfCheckComparison
from src.app.runner import Limits, Runner
from src.app.service import EXIT_CODES, AnalysisOutcome, AnalysisService, load_report
from src.contracts import AnalysisStatus

DEFAULT_ARTIFACTS = Path("artifacts/runs")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m src.app.cli", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    analyze = sub.add_parser("analyze", help="run one benchmark variant and compare it")
    analyze.add_argument("manifest", type=Path)
    analyze.add_argument("--variant", required=True, choices=("correct", "mutant"))
    analyze.add_argument("--schedule", required=True)
    analyze.add_argument("--simulator", choices=("verilator", "icarus"),
                         help="request a simulator; refused unless it is the manifest's")
    analyze.add_argument("--artifacts", type=Path, default=DEFAULT_ARTIFACTS)
    analyze.add_argument("--timeout", type=float, default=180.0, help="wall-clock seconds per job")
    analyze.add_argument("--fixture-causal", type=Path, metavar="MOCK_FIXTURE_JSON",
                         help="explicit fixture mode: attach a hand-authored causal report "
                              "(labeled synthetic; refused unless it matches the failure)")
    analyze.add_argument("--json", action="store_true", help="print the run record as JSON")

    open_ = sub.add_parser("open", help="validate and summarize a saved run record")
    open_.add_argument("report", type=Path)
    open_.add_argument("--json", action="store_true")
    return parser


def _summary(outcome: AnalysisOutcome) -> str:
    lines = [f"{outcome.case_id or '?'} {outcome.variant}/{outcome.schedule}: {outcome.status.value}"]
    lines.append(f"  comparison: {outcome.comparison_adapter[0]} ({outcome.comparison_adapter[1]})")
    for mode, result in outcome.comparisons.items():
        line = f"  {mode.value}: {result.status.value}, {result.compared_values} values compared"
        f = result.failure
        if f is not None:
            exp = f.expected.bits if f.expected else "none"
            act = f.actual.bits if f.actual else "none"
            line += (
                f"; first failure {f.kind.value} {f.signal_path} epoch {f.observation.epoch} "
                f"cycle {f.observation.cycle} {f.observation.phase.value} @ {f.observation.time_ps} ps "
                f"expected {exp} actual {act}"
            )
        lines.append(line)
    if outcome.causal is not None:
        kind = outcome.causal.provenance.kind.value
        lines.append(f"  causal: {len(outcome.causal.suspects)} ranked suspect(s) [{kind}]")
    for d in outcome.diagnostics:
        lines.append(f"  {d.code}: {d.message}")
    if outcome.fixture_mode:
        lines.append("  NOTE: fixture mode — causal data is hand-authored, not analysis")
    if outcome.report_path is not None:
        lines.append(f"  record: {outcome.report_path}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "open":
        try:
            data = load_report(args.report)
        except (OSError, ValueError) as exc:
            print(f"invalid run record: {exc}", file=sys.stderr)
            return EXIT_CODES[AnalysisStatus.INVALID_INPUT]
        status = AnalysisStatus(str(data["status"]))
        if args.json:
            print(json.dumps(data, indent=2))
        else:
            print(f"{data.get('case_id')} {data.get('variant')}/{data.get('schedule')}: {status.value}")
            diagnostics = data.get("diagnostics")
            for diag in diagnostics if isinstance(diagnostics, list) else []:
                if isinstance(diag, dict):
                    print(f"  {diag.get('code')}: {diag.get('message')}")
        return EXIT_CODES[status]

    causal = FixtureCausal(args.fixture_causal) if args.fixture_causal else None
    service = AnalysisService(
        Runner(args.artifacts, Limits(wall_clock_s=args.timeout)),
        SelfCheckComparison(),
        causal,
        fixture_mode=causal is not None,
    )
    cancel = threading.Event()

    def on_interrupt(signum: int, frame: FrameType | None) -> None:
        cancel.set()

    previous = signal.signal(signal.SIGINT, on_interrupt)
    try:
        outcome = service.analyze(
            args.manifest, variant=args.variant, schedule=args.schedule,
            simulator=args.simulator, cancel=cancel,
        )
    finally:
        signal.signal(signal.SIGINT, previous)
    print(json.dumps(outcome.to_json(), indent=2) if args.json else _summary(outcome))
    return outcome.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
