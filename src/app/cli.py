"""WaveRoot Track C command line.

    python -m src.app.cli analyze MANIFEST --variant mutant --schedule directed
    python -m src.app.cli compare --trace TRACE --golden GOLDEN
    python -m src.app.cli open REPORT_JSON

Exit codes follow spec.md section 8.4: 0 pass, 1 fail, 2 invalid_input or
error, 3 unsupported or inconclusive. Ctrl-C cancels a running simulation and
reaps its process group; Track A's comparison step does not poll for
cancellation, so Ctrl-C takes effect after it (bounded by --fst-timeout for
FST traces).

``analyze`` compares with Track A's waveform engine by default
(``--comparison track-a``); ``--comparison self-check`` uses the labeled
benchmark self-check over testbench samples. ``compare`` runs Track A's engine
on a saved trace and golden using Track A's own options.
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

from src.app.adapters import ComparisonAdapter, FixtureCausal, SelfCheckComparison, WaveformComparison
from src.app.runner import Limits, Runner
from src.app.service import EXIT_CODES, AnalysisOutcome, AnalysisService, load_report
from src.contracts import AnalysisStatus
from src.waveform.cache import DEFAULT_CACHE_DIR
from src.waveform.options import (
    DEFAULT_FST_TIMEOUT_S,
    WaveformOptions,
    add_waveform_arguments,
    exit_code,
    options_from_namespace,
    run_comparison,
)

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
    analyze.add_argument("--comparison", choices=("track-a", "self-check"), default="track-a",
                         help="track-a: Track A's waveform engine (default); self-check: Track C's "
                              "benchmark checker over testbench samples")
    # Track A's option names, types and help. add_waveform_arguments is not used
    # here because it requires --trace/--golden, which analyze produces itself.
    analyze.add_argument("--keep", action="append", default=None, metavar="PATTERN",
                         help="index only signals whose hierarchical path matches PATTERN "
                              "(* within a component, ** across components, ? one character); repeatable")
    analyze.add_argument("--cache-dir", type=Path, default=None, metavar="PATH",
                         help=f"reuse parsed traces cached in PATH (for example {DEFAULT_CACHE_DIR}/); off by default")
    analyze.add_argument("--fst-timeout", dest="fst_timeout_s", type=_seconds, default=DEFAULT_FST_TIMEOUT_S,
                         metavar="SECONDS",
                         help=f"time limit for converting an FST trace with fst2vcd (default {DEFAULT_FST_TIMEOUT_S:g})")
    analyze.add_argument("--json", action="store_true", help="print the run record as JSON")

    compare = sub.add_parser("compare", help="compare a saved trace with a golden using Track A's engine")
    add_waveform_arguments(compare)

    open_ = sub.add_parser("open", help="validate and summarize a saved run record")
    open_.add_argument("report", type=Path)
    open_.add_argument("--json", action="store_true")
    return parser


def _seconds(text: str) -> float:
    # Reuse Track A's validation by building its options; keeps one definition.
    # The dummy paths are never opened. If Track A starts validating paths in
    # WaveformOptions, ask it to export its seconds parser and import that.
    try:
        return WaveformOptions(trace=Path("-"), golden=Path("-"), fst_timeout_s=float(text)).fst_timeout_s
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a positive, finite number of seconds") from None


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
        # Track A reports why a comparison could not run inside the result.
        for d in result.diagnostics:
            lines.append(f"    {d.code}: {d.message}")
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
    parser = _parser()
    args = parser.parse_args(argv)
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

    if args.command == "compare":
        result = run_comparison(options_from_namespace(args))
        print(result.model_dump_json(indent=2))
        return exit_code(result)

    comparison: ComparisonAdapter
    if args.comparison == "track-a":
        comparison = WaveformComparison(
            keep=tuple(args.keep or ()), cache_dir=args.cache_dir, fst_timeout_s=args.fst_timeout_s
        )
    else:
        ignored = [flag for flag, value, default in (
            ("--keep", args.keep, None), ("--cache-dir", args.cache_dir, None),
            ("--fst-timeout", args.fst_timeout_s, DEFAULT_FST_TIMEOUT_S),
        ) if value != default]
        if ignored:
            parser.error(f"{', '.join(ignored)} apply only to --comparison track-a")
        comparison = SelfCheckComparison()
    causal = FixtureCausal(args.fixture_causal) if args.fixture_causal else None
    service = AnalysisService(
        Runner(args.artifacts, Limits(wall_clock_s=args.timeout)),
        comparison,
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
