"""Injectable comparison and causal-analysis adapters for the Track C service.

The service depends only on these protocols and on the types in
``src/contracts.py``. Track A's provider/comparison and Track B's causal engine
plug in here once they exist on ``main``; until then the available adapters
are labeled by ``kind`` so nothing synthetic can pass for analysis:

* ``analysis`` — a real engine: Track A's waveform comparison.
* ``benchmark_self_check`` — Track C's independent benchmark checker over
  testbench-sampled values (``src/app/selfcheck.py``). Real simulation data,
  but not Track A's waveform analysis.
* ``synthetic_fixture`` — hand-authored data. Only usable when the service is
  constructed in explicit fixture mode, and it refuses to describe a failure
  other than the one it was authored for.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Literal, Protocol

from src.app import selfcheck
from src.app.runner import RunRecord
from src.contracts import (
    CausalReport,
    ComparisonMode,
    ComparisonResult,
    FailureAnchor,
    GoldenTrace,
    load_mock_fixture,
)
from src.waveform.options import DEFAULT_FST_TIMEOUT_S, WaveformOptions, run_comparison

AdapterKind = Literal["analysis", "benchmark_self_check", "synthetic_fixture"]


class AdapterError(RuntimeError):
    """The adapter could not produce a result for this input."""


class ComparisonAdapter(Protocol):
    name: str
    kind: AdapterKind

    def compare(self, golden: GoldenTrace, run: RunRecord) -> ComparisonResult: ...


class CausalAdapter(Protocol):
    name: str
    kind: AdapterKind

    def analyze(
        self, failure: FailureAnchor, run: RunRecord, sources: Sequence[Path]
    ) -> CausalReport: ...


class SelfCheckComparison:
    """Benchmark self-check over ``observed.jsonl``. Raises
    ``selfcheck.ObservedFormatError`` for missing or malformed samples."""

    name = "track_c_selfcheck"
    kind: AdapterKind = "benchmark_self_check"

    def compare(self, golden: GoldenTrace, run: RunRecord) -> ComparisonResult:
        if run.observed is None:
            raise selfcheck.ObservedFormatError("run produced no observed samples")
        edges = selfcheck.parse_observed(run.observed)
        if golden.header.mode is ComparisonMode.CYCLE:
            return selfcheck.check_cycle(golden, edges)
        return selfcheck.check_handshake(golden, edges)


class WaveformComparison:
    """Track A's waveform engine (``src.waveform``) comparing the run's trace
    against the golden. The golden handed in is already rebased onto the
    trace's hierarchy; it is written next to the trace so the comparison can be
    reproduced with ``cli compare``. Input problems come back as ``error`` /
    ``invalid_input`` results, never exceptions."""

    name = "track_a_waveform"
    kind: AdapterKind = "analysis"

    def __init__(
        self,
        *,
        keep: tuple[str, ...] = (),
        cache_dir: Path | None = None,
        fst_timeout_s: float = DEFAULT_FST_TIMEOUT_S,
    ) -> None:
        self.keep = keep
        self.cache_dir = cache_dir
        self.fst_timeout_s = fst_timeout_s

    def compare(self, golden: GoldenTrace, run: RunRecord) -> ComparisonResult:
        if run.trace is None:
            raise AdapterError("the run produced no trace to compare")
        if run.run_dir is None:
            raise AdapterError("the run has no run directory to hold the golden")
        # The service already saves the rebased golden here; writing it again
        # keeps the adapter usable on its own and guarantees Track A compares
        # exactly the golden it was handed.
        golden_path = run.run_dir / f"golden_{golden.header.mode.value}.jsonl"
        text = golden.to_jsonl()
        if not golden_path.exists() or golden_path.read_text() != text:
            golden_path.write_text(text)
        return run_comparison(
            WaveformOptions(
                trace=run.trace,
                golden=golden_path,
                keep=self.keep,
                cache_dir=self.cache_dir,
                fst_timeout_s=self.fst_timeout_s,
            )
        )


class FixtureCausal:
    """Returns the hand-authored causal report from a mock fixture file.

    It is only accurate for the exact failure it was authored for, so any
    other failure is refused rather than decorated with an unrelated graph.
    """

    kind: AdapterKind = "synthetic_fixture"

    def __init__(self, fixture_path: Path) -> None:
        self.name = f"fixture:{fixture_path.name}"
        self._report = load_mock_fixture(fixture_path).report

    def analyze(
        self, failure: FailureAnchor, run: RunRecord, sources: Sequence[Path]
    ) -> CausalReport:
        authored = self._report.failure
        # Compare everything that identifies the failure; ``detail`` is prose.
        if authored is None or authored.model_dump(exclude={"detail"}) != failure.model_dump(
            exclude={"detail"}
        ):
            raise AdapterError(
                "the fixture report describes a different failure; "
                "refusing to attach it to this run"
            )
        return self._report
