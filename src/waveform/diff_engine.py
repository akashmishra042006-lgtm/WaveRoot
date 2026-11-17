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

import json
from dataclasses import dataclass, field
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
    LogicValue,
    Observation,
    OutOfRangeError,
    SamplingPhase,
    SignalNotFoundError,
    UnsupportedPrecisionError,
    WaveformProvider,
    parse_golden_jsonl,
)
from src.waveform.parser import InvalidTraceError, UnsupportedTraceError, VcdWaveformProvider

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
    notes: list[Diagnostic] = field(default_factory=list)

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
        return ComparisonResult(
            schema_version=1,
            status=status,
            mode=self.mode,
            failure=failures[0] if failures else None,
            additional_mismatches=tuple(failures[1:]),
            compared_values=self.compared,
            diagnostics=tuple([*self.notes, *(diagnostics or [])]),
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


def _clock_edges(run: _Run) -> tuple[tuple[int, ...], bool]:
    """Rising edges up to the first dump gap, and whether a gap cut the scan."""
    header = run.golden.header
    coverage = run.provider.get_coverage()
    whole = run.provider.get_window_slice([header.clock], coverage.start_ps, coverage.end_ps + 1)
    scan_end = whole.unavailable[0].start_ps if whole.unavailable else coverage.end_ps + 1
    if scan_end <= coverage.start_ps:
        return (), True
    try:
        edges = run.provider.get_clock_edges(header.clock, coverage.start_ps, scan_end)
    except IndeterminateClockError as error:
        raise _Stop(AnalysisStatus.INCONCLUSIVE, "CLOCK_INDETERMINATE", str(error)) from error
    return edges, bool(whole.unavailable)


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

    edges, cut_by_gap = _clock_edges(run)
    active = header.reset_active
    seen_active = False
    in_epoch = False
    epoch = -1
    cycle = 0
    consumed = 0  # handshake transactions matched in the current epoch
    final_done = False  # the final golden epoch needs nothing more

    for time_ps in edges:
        reset_before = provider.get_value_at(header.reset, time_ps, phase=_BEFORE).bits
        if reset_before == active:
            seen_active = True
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
        if provider.get_value_at(header.reset, time_ps, phase=_SETTLED).bits != reset_before:
            raise _Stop(
                AnalysisStatus.INCONCLUSIVE,
                "RESET_AT_EDGE",
                f"reset {header.reset} changes at the clock edge at {time_ps} ps",
            )
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

    return _finish(run, seen_active, epoch, final_epoch, final_done, cut_by_gap)


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
        run.notes.append(
            Diagnostic(
                code="EXPECTATIONS_CANCELLED_BY_RESET",
                message=f"reset ended epoch {epoch} with {len(records) - consumed} "
                "expected transaction(s) not yet transferred; they were cancelled",
            )
        )


def _finish(
    run: _Run, seen_active: bool, epoch: int, final_epoch: int, final_done: bool, cut_by_gap: bool
) -> ComparisonResult:
    if not seen_active:
        raise _Stop(
            AnalysisStatus.UNSUPPORTED,
            "NO_RESET_OBSERVED",
            f"reset {run.golden.header.reset} was never observed active before a clock edge",
        )
    if run.mode is ComparisonMode.HANDSHAKE and final_done and epoch >= final_epoch:
        if cut_by_gap:
            raise _Stop(
                AnalysisStatus.INCONCLUSIVE,
                "TRACE_GAP",
                "every expected transaction matched, but a dump gap prevents ruling out "
                "extra transactions afterwards",
            )
        return run.result(AnalysisStatus.PASS)
    if cut_by_gap:
        raise _Stop(
            AnalysisStatus.INCONCLUSIVE,
            "TRACE_GAP",
            "a dump gap interrupts the trace before every expectation was observed",
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
    """Load a VCD and a golden JSONL file and compare them.

    Unreadable files are ``error``; a malformed golden file or VCD is
    ``invalid_input``; sub-picosecond or unrepresentable traces are
    ``unsupported``. When the golden file cannot be parsed, the result's
    ``mode`` is taken from its first line if possible and is otherwise ``cycle``.
    """
    try:
        golden_text = golden_path.read_text(encoding="utf-8")
        vcd_text = vcd_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        return _file_result(AnalysisStatus.ERROR, ComparisonMode.CYCLE, "FILE_UNREADABLE", str(error))
    try:
        golden = parse_golden_jsonl(golden_text)
    except ValueError as error:
        return _file_result(AnalysisStatus.INVALID_INPUT, _mode_hint(golden_text), "GOLDEN_INVALID", str(error))
    mode = golden.header.mode
    try:
        provider = VcdWaveformProvider.from_text(vcd_text)
    except InvalidTraceError as error:
        return _file_result(AnalysisStatus.INVALID_INPUT, mode, "TRACE_INVALID", str(error))
    except (UnsupportedPrecisionError, UnsupportedTraceError) as error:
        return _file_result(AnalysisStatus.UNSUPPORTED, mode, "TRACE_UNSUPPORTED", str(error))
    return compare(provider, golden)
