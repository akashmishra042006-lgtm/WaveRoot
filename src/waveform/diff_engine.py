"""Golden-trace comparison for Track A.

``compare`` walks the actual rising clock edges of a trace, tracks reset epochs,
and checks a ``GoldenTrace`` against any ``WaveformProvider``. It stops at the
first divergence and returns a ``ComparisonResult``. The rules are those of
``spec.md`` sections 5, 7, 8, and 10:

* Reset is sampled ``before`` each rising edge. Epoch 0 starts at the first edge
  whose pre-edge reset is known inactive after reset was observed active.
* Cycle mode compares each listed signal at the header's sampling phase.
* Handshake mode samples valid, ready, and payload ``before`` the edge. A stall
  consumes no expectation, and the engine never searches ahead for a later
  matching payload.
* Several mismatches at one observation are ordered by golden declaration order.
* Missing evidence yields ``inconclusive``; it is never turned into a pass.

``compare_files`` adds file loading and maps malformed or unsupported input to
the corresponding statuses.
"""

from __future__ import annotations

import bisect
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from src.contracts import (
    AnalysisStatus,
    ComparisonMode,
    ComparisonResult,
    DataUnavailableError,
    Diagnostic,
    FailureAnchor,
    FailureKind,
    GoldenRecord,
    GoldenSignal,
    GoldenTrace,
    IndeterminateClockError,
    InvalidQueryError,
    LogicValue,
    Observation,
    OutOfRangeError,
    SamplingPhase,
    SignalNotFoundError,
    UnsupportedPrecisionError,
    WaveformProvider,
    parse_golden_jsonl,
)
from src.waveform.fst import ConversionFailedError, ConverterUnavailableError, load_trace
from src.waveform.parser import InvalidTraceError, UnsupportedTraceError

_BEFORE = SamplingPhase.BEFORE
_SETTLED = SamplingPhase.SETTLED


class _Stop(Exception):
    """Ends the comparison early with a non-failure status."""

    def __init__(self, status: AnalysisStatus, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.diagnostic = Diagnostic(code=code, message=message)


@dataclass(slots=True)
class _Run:
    provider: WaveformProvider
    golden: GoldenTrace
    compared: int = 0
    # Ascending times of clock transitions to or from x/z (spec 6.1: not edges).
    unknown_clock_times: tuple[int, ...] = ()
    # The first edge at which reset was observed active.
    active_from: int | None = None

    @property
    def mode(self) -> ComparisonMode:
        return self.golden.header.mode

    def result(
        self,
        status: AnalysisStatus,
        *,
        failures: list[FailureAnchor] | None = None,
        diagnostics: list[Diagnostic] | None = None,
    ) -> ComparisonResult:
        failures = failures or []
        extra = list(diagnostics or [])
        if status is AnalysisStatus.PASS and self.compared == 0:
            # A pass needs at least one compared value (spec 8.4), for example
            # when reset cancelled every expectation before any transfer.
            status = AnalysisStatus.INCONCLUSIVE
            extra.append(
                Diagnostic(
                    code="NOTHING_COMPARED",
                    message="no expected value was compared, so the trace cannot pass",
                )
            )
        return ComparisonResult(
            schema_version=1,
            status=status,
            mode=self.mode,
            failure=failures[0] if failures else None,
            additional_mismatches=tuple(failures[1:]),
            compared_values=self.compared,
            diagnostics=tuple(extra),
        )

    def require_known_clock(self, before_ps: int | None) -> None:
        """Spec 6.1: an x/z clock transition after reset is first observed active,
        and before ``before_ps`` (or anywhere later, if None), may hide an edge."""
        if self.active_from is None:
            return
        times = self.unknown_clock_times
        position = bisect.bisect_right(times, self.active_from)
        if position < len(times) and (before_ps is None or times[position] < before_ps):
            raise _Stop(
                AnalysisStatus.INCONCLUSIVE,
                "CLOCK_UNKNOWN_TRANSITION",
                f"clock {self.golden.header.clock} goes to or from x/z at {times[position]} ps "
                "after reset was observed; an edge may have been missed",
            )


def compare(provider: WaveformProvider, golden: GoldenTrace) -> ComparisonResult:
    """Compare ``golden`` against the trace behind ``provider``.

    Never raises for trace or golden content: every outcome is a
    ``ComparisonResult``. A missing or wrong-width signal is ``invalid_input``;
    a design with no observed reset is ``unsupported``; insufficient coverage,
    an unknown reset or handshake, an ambiguous clock, or a dump gap that
    interrupts the comparison is ``inconclusive``.
    """
    run = _Run(provider=provider, golden=golden)
    try:
        _check_signals(run)
        return _walk(run)
    except _Stop as stop:
        return run.result(stop.status, diagnostics=[stop.diagnostic])
    except SignalNotFoundError as error:
        return run.result(
            AnalysisStatus.INVALID_INPUT,
            diagnostics=[Diagnostic(code="SIGNAL_NOT_FOUND", message=str(error))],
        )
    except (DataUnavailableError, OutOfRangeError) as error:
        return run.result(
            AnalysisStatus.INCONCLUSIVE,
            diagnostics=[Diagnostic(code="DATA_UNAVAILABLE", message=str(error))],
        )


def _check_signals(run: _Run) -> None:
    header = run.golden.header
    control = [("clock", header.clock), ("reset", header.reset)]
    if header.valid is not None and header.ready is not None:
        control += [("valid", header.valid), ("ready", header.ready)]
    for role, path in control:
        width = run.provider.get_signal_decl(path).width
        if width != 1:
            raise _Stop(
                AnalysisStatus.INVALID_INPUT,
                "WIDTH_MISMATCH",
                f"{role} signal {path} is {width} bits wide; it must be 1 bit",
            )
    for signal in header.signals:
        width = run.provider.get_signal_decl(signal.path).width
        if width != signal.width:
            raise _Stop(
                AnalysisStatus.INVALID_INPUT,
                "WIDTH_MISMATCH",
                f"golden declares {signal.name} as {signal.width} bits but "
                f"{signal.path} is {width} bits in the trace",
            )


@dataclass(frozen=True, slots=True)
class _Cut:
    """Why the edge scan stops before the end of the trace."""

    code: str  # TRACE_GAP or CLOCK_INDETERMINATE
    detail: str


def _clock_edges(run: _Run) -> tuple[tuple[int, ...], _Cut | None]:
    """Rising edges in time order up to the first dump gap or ambiguous clock instant.

    Evidence is taken chronologically: a gap or same-timestamp clock glitch only
    limits the edges after it, so it cannot erase an earlier divergence.
    """
    header = run.golden.header
    provider = run.provider
    coverage = provider.get_coverage()
    whole = provider.get_window_slice([header.clock], coverage.start_ps, coverage.end_ps + 1)
    scan_end = whole.unavailable[0].start_ps if whole.unavailable else coverage.end_ps + 1
    cut = _Cut("TRACE_GAP", "") if whole.unavailable else None

    window = whole.signals[0]
    previous = window.initial.bits if window.initial is not None else "x"
    unknown: list[int] = []
    for sample in window.samples:
        if sample.time_ps >= scan_end:
            break
        if previous in ("x", "z") or sample.value.bits in ("x", "z"):
            unknown.append(sample.time_ps)
        previous = sample.value.bits
    run.unknown_clock_times = tuple(unknown)

    if scan_end <= coverage.start_ps:
        return (), cut
    try:
        return provider.get_clock_edges(header.clock, coverage.start_ps, scan_end), cut
    except IndeterminateClockError as error:
        ambiguous = _first_ambiguous_instant(run, coverage.start_ps, scan_end)
        edges = provider.get_clock_edges(header.clock, coverage.start_ps, ambiguous) if ambiguous > coverage.start_ps else ()
        return edges, _Cut("CLOCK_INDETERMINATE", str(error))


def _first_ambiguous_instant(run: _Run, start_ps: int, end_ps: int) -> int:
    """Smallest t in [start_ps, end_ps) such that edges over [start_ps, t] are ambiguous."""
    clock = run.golden.header.clock
    low, high = start_ps, end_ps - 1
    while low < high:
        middle = (low + high) // 2
        try:
            run.provider.get_clock_edges(clock, start_ps, middle + 1)
        except IndeterminateClockError:
            high = middle
        else:
            low = middle + 1
    return low


def _require_stable_reset(run: _Run, time_ps: int, reset_before: str) -> None:
    """Spec 7: a reset change at the edge timestamp makes the comparison inconclusive."""
    reset = run.golden.header.reset
    message = f"reset {reset} changes at the clock edge at {time_ps} ps"
    if run.provider.get_value_at(reset, time_ps, phase=_SETTLED).bits != reset_before:
        raise _Stop(AnalysisStatus.INCONCLUSIVE, "RESET_AT_EDGE", message)
    try:
        # Reset is one bit, so the edge query refuses several transitions at one
        # timestamp, such as a 1 -> 0 -> 1 pulse whose final value is unchanged.
        run.provider.get_clock_edges(reset, time_ps, time_ps + 1)
    except IndeterminateClockError as error:
        raise _Stop(AnalysisStatus.INCONCLUSIVE, "RESET_AT_EDGE", f"{message}: {error}") from error


def _walk(run: _Run) -> ComparisonResult:
    header = run.golden.header
    provider = run.provider
    handshake = run.mode is ComparisonMode.HANDSHAKE
    by_epoch: list[list[GoldenRecord]] = []
    for record in run.golden.records:
        if record.epoch == len(by_epoch):
            by_epoch.append([])
        by_epoch[record.epoch].append(record)
    final_epoch = len(by_epoch) - 1

    edges, cut = _clock_edges(run)
    active = header.reset_active
    seen_active = False
    in_epoch = False
    epoch = -1
    cycle = 0
    consumed = 0  # handshake transactions matched in the current epoch
    final_done = False  # the final golden epoch needs nothing more

    for time_ps in edges:
        run.require_known_clock(time_ps)
        reset_before = provider.get_value_at(header.reset, time_ps, phase=_BEFORE).bits
        if reset_before == active:
            if not seen_active:
                seen_active = True
                run.active_from = time_ps
            _require_stable_reset(run, time_ps, reset_before)
            if in_epoch:
                _close_epoch(run, epoch, cycle, consumed, by_epoch)
                final_done = final_done or epoch >= final_epoch
                in_epoch = False
            continue
        if reset_before not in "01":
            if seen_active:
                raise _Stop(
                    AnalysisStatus.INCONCLUSIVE,
                    "RESET_UNKNOWN",
                    f"reset {header.reset} is {reset_before!r} before the clock edge at {time_ps} ps",
                )
            continue
        if not seen_active:
            continue
        _require_stable_reset(run, time_ps, reset_before)
        if in_epoch:
            cycle += 1
        else:
            in_epoch, epoch, cycle, consumed = True, epoch + 1, 0, 0
        records = by_epoch[epoch] if epoch <= final_epoch else []

        if handshake:
            failures, consumed = _handshake_edge(run, records, epoch, cycle, time_ps, consumed)
            if failures:
                return run.result(AnalysisStatus.FAIL, failures=failures)
            if epoch == final_epoch and consumed == len(records):
                final_done = True
        elif cycle < len(records):
            failures = _compare_record(
                run, records[cycle], Observation(epoch=epoch, cycle=cycle, phase=header.sampling, time_ps=time_ps), None
            )
            if failures:
                return run.result(AnalysisStatus.FAIL, failures=failures)
            if epoch == final_epoch and cycle == len(records) - 1:
                return run.result(AnalysisStatus.PASS)

    return _finish(run, seen_active, epoch, final_epoch, final_done, cut)


def _close_epoch(
    run: _Run, epoch: int, cycle: int, consumed: int, by_epoch: list[list[GoldenRecord]]
) -> None:
    """Handle a reset assertion that ends an active epoch."""
    if epoch >= len(by_epoch):
        return
    records = by_epoch[epoch]
    if run.mode is ComparisonMode.CYCLE:
        if cycle < len(records) - 1:
            raise _Stop(
                AnalysisStatus.INCONCLUSIVE,
                "EPOCH_ENDED_EARLY",
                f"reset ended epoch {epoch} after cycle {cycle}, but the golden trace "
                f"lists cycles up to {len(records) - 1}",
            )
    elif consumed < len(records):
        # Spec 7: cancelled, never matched against a later epoch, but not waived.
        raise _Stop(
            AnalysisStatus.INCONCLUSIVE,
            "EXPECTATIONS_CANCELLED_BY_RESET",
            f"reset ended epoch {epoch} with {len(records) - consumed} expected "
            "transaction(s) not yet transferred",
        )


def _finish(
    run: _Run, seen_active: bool, epoch: int, final_epoch: int, final_done: bool, cut: _Cut | None
) -> ComparisonResult:
    if cut is not None and cut.code == "CLOCK_INDETERMINATE":
        what = f"an ambiguous clock ({cut.detail})"
    else:
        what = "a dump gap"
    if not seen_active:
        if cut is not None and cut.code == "CLOCK_INDETERMINATE":
            raise _Stop(AnalysisStatus.INCONCLUSIVE, cut.code, cut.detail)
        raise _Stop(
            AnalysisStatus.UNSUPPORTED,
            "NO_RESET_OBSERVED",
            f"reset {run.golden.header.reset} was never observed active before a clock edge",
        )
    if run.mode is ComparisonMode.HANDSHAKE and final_done and epoch >= final_epoch:
        if cut is not None:
            raise _Stop(
                AnalysisStatus.INCONCLUSIVE,
                cut.code,
                f"every expected transaction matched, but {what} prevents ruling out "
                "extra transactions afterwards",
            )
        # Ruling out extra transactions needs every later edge, so an unknown
        # clock anywhere after reset also blocks the pass.
        run.require_known_clock(None)
        return run.result(AnalysisStatus.PASS)
    if cut is not None:
        raise _Stop(
            AnalysisStatus.INCONCLUSIVE,
            cut.code,
            f"{what} interrupts the trace before every expectation was observed",
        )
    raise _Stop(
        AnalysisStatus.INCONCLUSIVE,
        "TRACE_ENDED_EARLY",
        "the trace ended before every expectation was observed"
        + ("" if run.golden.header.deadline_cycle is not None or run.mode is ComparisonMode.CYCLE
           else "; the golden trace sets no deadline_cycle"),
    )


def _handshake_edge(
    run: _Run, records: list[GoldenRecord], epoch: int, cycle: int, time_ps: int, consumed: int
) -> tuple[list[FailureAnchor], int]:
    header = run.golden.header
    assert header.valid is not None and header.ready is not None
    observation = Observation(epoch=epoch, cycle=cycle, phase=_BEFORE, time_ps=time_ps)
    valid = run.provider.get_value_at(header.valid, time_ps, phase=_BEFORE).bits
    ready = run.provider.get_value_at(header.ready, time_ps, phase=_BEFORE).bits
    if valid != "0" and ready != "0":
        if valid != "1" or ready != "1":
            raise _Stop(
                AnalysisStatus.INCONCLUSIVE,
                "HANDSHAKE_UNKNOWN",
                f"valid={valid!r} ready={ready!r} before the clock edge at {time_ps} ps; "
                "a transfer cannot be established",
            )
        if consumed >= len(records):
            anchor_signal = header.signals[0]
            actual = run.provider.get_value_at(anchor_signal.path, time_ps, phase=_BEFORE)
            return [
                FailureAnchor(
                    kind=FailureKind.EXTRA_TRANSACTION,
                    mode=ComparisonMode.HANDSHAKE,
                    signal_path=anchor_signal.path,
                    clock_path=header.clock,
                    observation=observation,
                    transaction_index=consumed,
                    expected=None,
                    actual=actual,
                    detail=f"a transfer occurred at epoch {epoch} cycle {cycle} ({time_ps} ps) "
                    f"but the golden trace expects only {len(records)} transaction(s) in this epoch",
                )
            ], consumed
        failures = _compare_record(run, records[consumed], observation, consumed)
        if failures:
            return failures, consumed
        consumed += 1
    deadline = header.deadline_cycle
    is_final = bool(records) and records[0].epoch == run.golden.records[-1].epoch
    if deadline is not None and is_final and cycle == deadline and consumed < len(records):
        anchor_signal = header.signals[0]
        expected_bits = records[consumed].values[anchor_signal.name]
        return [
            FailureAnchor(
                kind=FailureKind.MISSING_TRANSACTION,
                mode=ComparisonMode.HANDSHAKE,
                signal_path=anchor_signal.path,
                clock_path=header.clock,
                observation=observation,
                transaction_index=consumed,
                expected=LogicValue(width=anchor_signal.width, bits=expected_bits),
                actual=None,
                detail=f"transaction {consumed} of epoch {epoch} had not transferred by the "
                f"deadline at cycle {deadline} ({time_ps} ps)",
            )
        ], consumed
    return [], consumed


def _compare_record(
    run: _Run, record: GoldenRecord, observation: Observation, transaction_index: int | None
) -> list[FailureAnchor]:
    """Compare one record's values in golden declaration order."""
    header = run.golden.header
    failures: list[FailureAnchor] = []
    for signal in header.signals:
        expected_bits = record.values.get(signal.name)
        if expected_bits is None:
            continue
        run.compared += 1
        actual = run.provider.get_value_at(signal.path, observation.time_ps, phase=observation.phase)
        if actual.bits == expected_bits:
            continue
        failures.append(_mismatch(header.clock, run.mode, signal, expected_bits, actual, observation, transaction_index))
    return failures


def _mismatch(
    clock: str,
    mode: ComparisonMode,
    signal: GoldenSignal,
    expected_bits: str,
    actual: LogicValue,
    observation: Observation,
    transaction_index: int | None,
) -> FailureAnchor:
    kind = FailureKind.VALUE_MISMATCH if actual.is_fully_known else FailureKind.UNKNOWN_VALUE
    where = f"epoch {observation.epoch} cycle {observation.cycle}"
    if transaction_index is not None:
        where += f" transaction {transaction_index}"
    return FailureAnchor(
        kind=kind,
        mode=mode,
        signal_path=signal.path,
        clock_path=clock,
        observation=observation,
        transaction_index=transaction_index,
        expected=LogicValue(width=signal.width, bits=expected_bits),
        actual=actual,
        detail=f"{signal.name}: expected {expected_bits} but observed {actual.bits} at {where} "
        f"({observation.phase.value}, {observation.time_ps} ps)",
    )


# --------------------------------------------------------------------------
# File-level entry point
# --------------------------------------------------------------------------


def _mode_hint(golden_text: str) -> ComparisonMode:
    """Best-effort mode for a result about a golden file that failed to parse."""
    try:
        first = json.loads(golden_text.split("\n", 1)[0])
        return ComparisonMode(first["mode"])
    except (ValueError, KeyError, TypeError):
        return ComparisonMode.CYCLE


def _file_result(status: AnalysisStatus, mode: ComparisonMode, code: str, message: str) -> ComparisonResult:
    return ComparisonResult(
        schema_version=1,
        status=status,
        mode=mode,
        failure=None,
        compared_values=0,
        diagnostics=(Diagnostic(code=code, message=message),),
    )


def compare_files(vcd_path: Path, golden_path: Path) -> ComparisonResult:
    """Load a ``.vcd`` or ``.fst`` trace and a golden JSONL file and compare them.

    Unreadable files, a missing FST converter, and a failed conversion are
    ``error``; a malformed golden file or VCD is ``invalid_input``;
    sub-picosecond or unrepresentable traces, and trace suffixes other than
    ``.vcd`` and ``.fst``, are ``unsupported``. When the golden file cannot be
    parsed, the result's ``mode`` is taken from its first line if possible and
    is otherwise ``cycle``.
    """
    return _compare_with_loader(vcd_path, golden_path, lambda: load_trace(vcd_path))


def _compare_with_loader(
    trace_path: Path, golden_path: Path, load: Callable[[], WaveformProvider]
) -> ComparisonResult:
    """``compare_files`` with the trace loaded by ``load``; never raises for input problems."""
    try:
        golden_text = golden_path.read_text(encoding="utf-8")
        # Checked up front so an unreadable trace is reported before golden content.
        with trace_path.open("rb"):
            pass
    except (OSError, UnicodeDecodeError) as error:
        return _file_result(AnalysisStatus.ERROR, ComparisonMode.CYCLE, "FILE_UNREADABLE", str(error))
    try:
        golden = parse_golden_jsonl(golden_text)
    except ValueError as error:
        return _file_result(AnalysisStatus.INVALID_INPUT, _mode_hint(golden_text), "GOLDEN_INVALID", str(error))
    mode = golden.header.mode
    try:
        provider = load()
    except (OSError, UnicodeDecodeError) as error:
        return _file_result(AnalysisStatus.ERROR, mode, "FILE_UNREADABLE", str(error))
    except InvalidTraceError as error:
        return _file_result(AnalysisStatus.INVALID_INPUT, mode, "TRACE_INVALID", str(error))
    except InvalidQueryError as error:
        # Only a signal-selection (keep) pattern can raise this while loading.
        return _file_result(AnalysisStatus.INVALID_INPUT, mode, "SELECTION_INVALID", str(error))
    except (UnsupportedPrecisionError, UnsupportedTraceError) as error:
        return _file_result(AnalysisStatus.UNSUPPORTED, mode, "TRACE_UNSUPPORTED", str(error))
    except ConverterUnavailableError as error:
        return _file_result(AnalysisStatus.ERROR, mode, "CONVERTER_UNAVAILABLE", str(error))
    except ConversionFailedError as error:
        return _file_result(AnalysisStatus.ERROR, mode, "CONVERSION_FAILED", str(error))
    return compare(provider, golden)
