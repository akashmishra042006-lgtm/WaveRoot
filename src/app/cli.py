"""WaveRoot Track C command line.

    python -m src.app.cli analyze MANIFEST --variant mutant --schedule directed
    python -m src.app.cli compare --trace TRACE --golden GOLDEN
    python -m src.app.cli open REPORT_JSON
    python -m src.app.cli serve [--root benchmarks] [--port 8765]

Exit codes follow spec.md section 8.4: 0 pass, 1 fail, 2 invalid_input or
error, 3 unsupported or inconclusive. Ctrl-C cancels a running simulation and
reaps its process group. A comparison step already in progress finishes first;
the cancel then stops the run as ``error``/``CANCELLED`` before any verdict.
``--timeout`` covers the simulation, not the comparison.

``analyze`` compares with Track A's waveform engine by default
(``--comparison track-a``); ``--comparison self-check`` uses the labeled
benchmark self-check over testbench samples. ``compare`` runs Track A's engine
on a saved trace and golden using Track A's own options.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
from collections.abc import Sequence
from pathlib import Path
from types import FrameType

from src.app.api import DEFAULT_DEV_ORIGINS, ApiConfig, ApiServer
from src.app.adapters import (
    CausalAdapter,
    ComparisonAdapter,
    FixtureCausal,
    SelfCheckComparison,
    TrackBCausal,
    WaveformComparison,
)
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
    parser = argparse.ArgumentParser(prog="waveroot", description=__doc__.split("\n\n")[0])
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
    analyze.add_argument("--causal", choices=("track-b", "none"), default="track-b",
                         help="track-b: Track B's causal engine on a failing run (default); none: report "
                              "the failure unlocalized. Ignored when --fixture-causal is given")
    analyze.add_argument("--causal-timeout", dest="causal_timeout_s", type=_seconds, default=120.0,
                         metavar="SECONDS",
                         help="wall-clock limit for the causal step, which runs in a child process that "
                              "is stopped on this limit or on Ctrl-C (default 120); a timeout leaves the "
                              "failure verdict unchanged and unlocalized")
    analyze.add_argument("--comparison", choices=("track-a", "self-check"), default="track-a",
                         help="track-a: Track A's waveform engine (default); self-check: Track C's "
                              "benchmark checker over testbench samples")
    # Track A's option names, types and help. add_waveform_arguments is not used
    # here because it requires --trace/--golden, which analyze produces itself.
    analyze.add_argument("--keep", action="append", default=None, metavar="PATTERN",
                         help="index only signals whose hierarchical path matches PATTERN "
                              "(* within a component, ** across components, ? one character); repeatable. "
                              "Patterns match the trace's own paths (Verilator adds TOP., e.g. 'TOP.tb.**'); "
                              "excluding a compared signal is invalid_input, never a pass")
    analyze.add_argument("--cache-dir", type=Path, default=None, metavar="PATH",
                         help=f"reuse parsed traces cached in PATH (for example {DEFAULT_CACHE_DIR}/); off by default")
    analyze.add_argument("--fst-timeout", dest="fst_timeout_s", type=_seconds, default=None,
                         metavar="SECONDS",
                         help="time limit for converting an FST trace with fst2vcd "
                              f"(default {DEFAULT_FST_TIMEOUT_S:g}); benchmark runs write VCD, so it "
                              "applies only to FST traces")
    analyze.add_argument("--json", action="store_true", help="print the run record as JSON")

    compare = sub.add_parser("compare", help="compare a saved trace with a golden using Track A's engine")
    add_waveform_arguments(compare)

    serve = sub.add_parser("serve", help="start the local API (127.0.0.1 only)")
    serve.add_argument("--root", type=Path, default=Path("benchmarks"),
                       help="directory holding the benchmark cases the API may run")
    serve.add_argument("--artifacts", type=Path, default=DEFAULT_ARTIFACTS)
    serve.add_argument("--host", default="127.0.0.1", help="a loopback IPv4 address")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--allow-origin", action="append", default=[], metavar="ORIGIN",
                       help="extra web origin allowed to call the API; repeatable")
    serve.add_argument("--dev", action="store_true",
                       help="also allow the Vite dev/preview origins (" + ", ".join(DEFAULT_DEV_ORIGINS) + ")")
    serve.add_argument("--timeout", type=float, default=180.0, help="wall-clock seconds per job")
    serve.add_argument("--jobs", type=int, default=1, help="jobs that may run at once")
    serve.add_argument("--token-file", type=Path, default=None, metavar="PATH",
                       help="also write this start's access token to PATH (created with mode 0600; "
                            "an existing file is replaced) for scripts")

    open_ = sub.add_parser("open", help="validate and summarize a saved run record")
    open_.add_argument("report", type=Path)
    open_.add_argument("--json", action="store_true")
    return parser


def _write_private(path: Path, text: str) -> None:
    """Write ``text`` to a file only the user can read. An existing file is
    removed first, so a looser mode or another owner is never inherited."""
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(text)


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

    if args.command == "serve":
        try:
            server = ApiServer(ApiConfig(
                root=args.root, artifacts=args.artifacts, host=args.host, port=args.port,
                extra_origins=tuple(args.allow_origin) + (DEFAULT_DEV_ORIGINS if args.dev else ()),
                timeout_s=args.timeout, max_concurrent=args.jobs,
            ))
        except (ValueError, OSError) as exc:
            print(f"cannot start the API: {exc}", file=sys.stderr)
            return 2
        if args.token_file is not None:
            try:
                _write_private(args.token_file, server.token + "\n")
            except OSError as exc:
                print(f"cannot write the token file: {exc}", file=sys.stderr)
                return 2
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
        print(f"WaveRoot API on {server.origin}  (cases under {args.root}; Ctrl-C to stop)", flush=True)
        print("allowed origins: " + ", ".join(sorted(server.allowed_origins())), flush=True)
        print(f"access token (new each start): {server.token}", flush=True)
        print(f"UI: open the page with ?api={server.origin}#token={server.token}", flush=True)
        server.serve_forever()
        return 0

    if args.command == "compare":
        result = run_comparison(options_from_namespace(args))
        print(result.model_dump_json(indent=2))
        return exit_code(result)

    comparison: ComparisonAdapter
    if args.comparison == "track-a":
        comparison = WaveformComparison(
            keep=tuple(args.keep or ()), cache_dir=args.cache_dir,
            fst_timeout_s=DEFAULT_FST_TIMEOUT_S if args.fst_timeout_s is None else args.fst_timeout_s,
        )
    else:
        ignored = [flag for flag, value, default in (
            ("--keep", args.keep, None), ("--cache-dir", args.cache_dir, None),
            ("--fst-timeout", args.fst_timeout_s, None),
        ) if value != default]
        if ignored:
            parser.error(f"{', '.join(ignored)} apply only to --comparison track-a")
        comparison = SelfCheckComparison()
    causal: CausalAdapter | None
    if args.fixture_causal:
        causal = FixtureCausal(args.fixture_causal)
    else:
        causal = TrackBCausal() if args.causal == "track-b" else None
    service = AnalysisService(
        Runner(args.artifacts, Limits(wall_clock_s=args.timeout)),
        comparison,
        causal,
        fixture_mode=bool(args.fixture_causal),
        causal_timeout_s=args.causal_timeout_s,
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
