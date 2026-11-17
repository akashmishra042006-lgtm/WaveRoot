"""Track A's command-line options for golden-trace comparison.

Track A owns the option names and their behavior; Track C wires them into the
command in ``src/app/cli.py``. Nothing here prints, exits, or defines an entry
point: a caller adds the arguments, builds ``WaveformOptions`` from the parsed
namespace, runs ``run_comparison``, and exits with ``exit_code(result)``.
"""

from __future__ import annotations

import argparse
import math
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final

from src.contracts import AnalysisStatus, ComparisonResult, WaveformProvider
from src.waveform.cache import DEFAULT_CACHE_DIR, load_cached
from src.waveform.diff_engine import _compare_with_loader
from src.waveform.fst import load_trace

DEFAULT_FST_TIMEOUT_S: Final = 120.0

#: ``spec.md`` section 8.4.
EXIT_CODES: Final = MappingProxyType(
    {
        AnalysisStatus.PASS: 0,
        AnalysisStatus.FAIL: 1,
        AnalysisStatus.INVALID_INPUT: 2,
        AnalysisStatus.ERROR: 2,
        AnalysisStatus.UNSUPPORTED: 3,
        AnalysisStatus.INCONCLUSIVE: 3,
    }
)


def _check_timeout(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("fst_timeout_s must be a number of seconds")
    if not math.isfinite(value) or value <= 0:
        raise ValueError("fst_timeout_s must be a positive, finite number of seconds")
    return float(value)


@dataclass(frozen=True, slots=True)
class WaveformOptions:
    """Inputs for one golden-trace comparison.

    ``keep`` empty means every signal is indexed. ``cache_dir`` ``None`` means
    the parsed-trace cache is not used.
    """

    trace: Path
    golden: Path
    keep: tuple[str, ...] = ()
    cache_dir: Path | None = None
    fst_timeout_s: float = DEFAULT_FST_TIMEOUT_S

    def __post_init__(self) -> None:
        if not isinstance(self.keep, tuple) or not all(isinstance(p, str) for p in self.keep):
            raise TypeError("keep must be a tuple of pattern strings")
        object.__setattr__(self, "fst_timeout_s", _check_timeout(self.fst_timeout_s))


def _seconds(text: str) -> float:
    try:
        return _check_timeout(float(text))
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a positive, finite number of seconds") from None


def add_waveform_arguments(parser: argparse.ArgumentParser) -> None:
    """Add ``--trace``, ``--golden``, ``--keep``, ``--cache-dir``, and ``--fst-timeout``."""
    group = parser.add_argument_group("waveform comparison")
    group.add_argument(
        "--trace", type=Path, required=True, metavar="PATH", help="simulation trace (.vcd or .fst)"
    )
    group.add_argument(
        "--golden", type=Path, required=True, metavar="PATH", help="golden expectations (JSONL)"
    )
    group.add_argument(
        "--keep",
        action="append",
        default=None,
        metavar="PATTERN",
        help="index only signals whose hierarchical path matches PATTERN "
        "(* within a component, ** across components, ? one character); repeatable",
    )
    group.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        metavar="PATH",
        help=f"reuse parsed traces cached in PATH (for example {DEFAULT_CACHE_DIR}/); off by default",
    )
    group.add_argument(
        "--fst-timeout",
        dest="fst_timeout_s",
        type=_seconds,
        default=DEFAULT_FST_TIMEOUT_S,
        metavar="SECONDS",
        help=f"time limit for converting an FST trace with fst2vcd (default {DEFAULT_FST_TIMEOUT_S:g})",
    )


def options_from_namespace(namespace: argparse.Namespace) -> WaveformOptions:
    """Build ``WaveformOptions`` from a namespace parsed with ``add_waveform_arguments``."""
    return WaveformOptions(
        trace=namespace.trace,
        golden=namespace.golden,
        keep=tuple(namespace.keep or ()),
        cache_dir=namespace.cache_dir,
        fst_timeout_s=namespace.fst_timeout_s,
    )


def run_comparison(options: WaveformOptions) -> ComparisonResult:
    """Load the trace (FST conversion, signal selection, cache) and compare it.

    Never raises for input problems: unreadable files, a missing converter, or a
    failed conversion are ``error``; malformed traces, golden files, or
    ``keep`` patterns are ``invalid_input``; unsupported traces are
    ``unsupported``.
    """
    keep = options.keep or None

    def load() -> WaveformProvider:
        if options.cache_dir is not None:
            return load_cached(
                options.trace, cache_dir=options.cache_dir, keep=keep, timeout_s=options.fst_timeout_s
            )
        return load_trace(options.trace, timeout_s=options.fst_timeout_s, keep=keep)

    return _compare_with_loader(options.trace, options.golden, load)


def exit_code(result: ComparisonResult) -> int:
    """Process exit status for ``result`` per ``spec.md`` section 8.4."""
    return EXIT_CODES[result.status]
