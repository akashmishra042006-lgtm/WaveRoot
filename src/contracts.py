"""WaveRoot shared contracts, schema version 1.

This module is the cross-track source of truth described in ``spec.md``. It
contains data models, the ``WaveformProvider`` protocol, typed errors, and a
fixture-backed ``MockWaveformProvider``. It deliberately contains no waveform
parser, comparison engine, dependency graph, or causal slicer, and it imports
nothing from ``src.waveform``, ``src.causal``, or ``src.app``.

Conventions (normative text is in ``spec.md``):

* Time is a non-negative integer number of picoseconds. Python code uses
  ``int``; JSON carries canonical decimal strings so values above 2**53 survive
  JavaScript consumers. Floats and non-canonical strings are rejected.
* A value is ``LogicValue(width, bits)`` with ``bits`` drawn from ``01xz``,
  most significant (declared left) bit first. X/Z are never converted silently.
* ``before`` at T is the final recorded state strictly before T. ``settled`` at
  T is the final recorded state at or before T, after every record carrying T.
* Query intervals are half-open ``[start_ps, end_ps)``. Trace coverage is the
  inclusive range ``[start_ps, end_ps]``.
* Models are immutable and reject unknown fields.
"""

from __future__ import annotations

import bisect
import re
from collections.abc import Iterable, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Final, Literal, Protocol, Self, runtime_checkable

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    model_validator,
)

SCHEMA_VERSION: Final = 1

# --------------------------------------------------------------------------
# Typed errors
# --------------------------------------------------------------------------


class ContractError(Exception):
    """Base class for every error defined by the shared contracts."""


class InvalidQueryError(ContractError):
    """The request itself is malformed.

    Examples: a non-integer or negative time, ``start_ps >= end_ps`` (empty
    windows are rejected), an empty or duplicated signal list, or a clock query
    on a signal that is not one bit wide.
    """


class SignalNotFoundError(ContractError):
    """The signal path does not resolve in the trace. Never reported as X or 0."""


class OutOfRangeError(ContractError):
    """A well-formed query refers to time outside the trace coverage."""


class DataUnavailableError(ContractError):
    """The trace covers the time, but dumping was off (``$dumpoff``) there."""


class UnsupportedPrecisionError(ContractError):
    """A timestamp cannot be represented exactly in integer picoseconds."""


class IndeterminateClockError(ContractError):
    """A clock has several transitions at one timestamp inside the queried range.

    Raised by providers that retain same-timestamp change history. The mock
    provider stores one final value per timestamp, so it cannot observe this.
    """


# --------------------------------------------------------------------------
# Scalar types
# --------------------------------------------------------------------------

_DECIMAL_RE: Final = re.compile(r"(0|[1-9][0-9]*)")
_PATH_COMPONENT: Final = r"[A-Za-z_][A-Za-z0-9_$]*(\[[0-9]+\])*"
_SIGNAL_PATH_RE: Final = re.compile(rf"{_PATH_COMPONENT}(\.{_PATH_COMPONENT})*")
_BINARY_RE: Final = re.compile(r"[01]+")

MAX_TIME_PS: Final = 2**128 - 1
"""Largest legal timestamp (spec section 4). Every 64-bit tick count at every
legal timescale fits; a trace with a larger timestamp is ``unsupported``."""
_MAX_TIME_DIGITS: Final = len(str(MAX_TIME_PS))  # 39


def _parse_time_ps(value: object) -> object:
    if isinstance(value, bool):
        raise ValueError("timestamp must be an integer, not a boolean")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        # Checked before int() so an absurdly long string never reaches the
        # interpreter's integer-conversion limit (spec section 11).
        if len(value) > _MAX_TIME_DIGITS:
            raise ValueError(f"timestamp string must have at most {_MAX_TIME_DIGITS} digits")
        if _DECIMAL_RE.fullmatch(value):
            return int(value)
        raise ValueError("timestamp string must be a canonical non-negative decimal")
    raise ValueError("timestamp must be an integer number of picoseconds")


def _time_to_json(value: int) -> str:
    return str(value)


def _check_signal_path(value: str) -> str:
    if not _SIGNAL_PATH_RE.fullmatch(value):
        raise ValueError(
            "signal path must be dot-separated simple identifiers with optional "
            "[index] suffixes; escaped identifiers are unsupported in schema 1"
        )
    return value


def _check_relative_path(value: str) -> str:
    parts = value.split("/")
    if (
        not value
        or value.startswith("/")
        or "\\" in value
        or ":" in value
        or any(part in ("", ".", "..") for part in parts)
    ):
        raise ValueError("source path must be a normalized relative POSIX path")
    return value


def _lowercase(value: object) -> object:
    return value.lower() if isinstance(value, str) else value


TimePs = Annotated[
    int,
    BeforeValidator(_parse_time_ps),
    Field(ge=0, le=MAX_TIME_PS),
    PlainSerializer(_time_to_json, return_type=str, when_used="json"),
]
"""Integer picoseconds in ``[0, MAX_TIME_PS]``; JSON form is a canonical decimal string."""

Count = Annotated[int, Field(strict=True, ge=0)]
PositiveInt = Annotated[int, Field(strict=True, ge=1)]
SignalPath = Annotated[str, AfterValidator(_check_signal_path)]
Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.:\-]+$")]
Text = Annotated[str, Field(min_length=1)]
Bits = Annotated[str, BeforeValidator(_lowercase), Field(pattern=r"^[01xz]+$")]


class TimeUnit(StrEnum):
    S = "s"
    MS = "ms"
    US = "us"
    NS = "ns"
    PS = "ps"
    FS = "fs"


_PS_PER_UNIT: Final[dict[TimeUnit, int]] = {
    TimeUnit.S: 10**12,
    TimeUnit.MS: 10**9,
    TimeUnit.US: 10**6,
    TimeUnit.NS: 10**3,
    TimeUnit.PS: 1,
}


def to_picoseconds(count: int, magnitude: int, unit: TimeUnit) -> int:
    """Convert ``count`` ticks of a ``magnitude unit`` timescale to exact ps.

    ``magnitude`` must be 1, 10, or 100 as in a VCD ``$timescale``. Arithmetic
    is exact integer arithmetic. A femtosecond result that is not a whole number
    of picoseconds raises ``UnsupportedPrecisionError``; nothing is rounded.
    """
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise InvalidQueryError("tick count must be a non-negative integer")
    if magnitude not in (1, 10, 100):
        raise InvalidQueryError("timescale magnitude must be 1, 10, or 100")
    ticks = count * magnitude
    if unit is TimeUnit.FS:
        whole, remainder = divmod(ticks, 1000)
        if remainder:
            raise UnsupportedPrecisionError(
                f"{count} x {magnitude} fs is not a whole number of picoseconds"
            )
        return whole
    return ticks * _PS_PER_UNIT[unit]


# --------------------------------------------------------------------------
# Enumerations
# --------------------------------------------------------------------------


class SamplingPhase(StrEnum):
    BEFORE = "before"
    SETTLED = "settled"


class ClockEdge(StrEnum):
    RISING = "rising"
    FALLING = "falling"


class EdgeType(StrEnum):
    COMBINATIONAL = "combinational"
    SEQUENTIAL = "sequential"


class EdgeRole(StrEnum):
    DATA = "data"
    CONTROL = "control"


class BranchStatus(StrEnum):
    RETAINED = "retained"
    PRUNED = "pruned"
    UNKNOWN = "unknown"


class ComparisonMode(StrEnum):
    CYCLE = "cycle"
    HANDSHAKE = "handshake"


class AnalysisStatus(StrEnum):
    """Outcome of a comparison or of a whole analysis run."""

    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"
    UNSUPPORTED = "unsupported"
    INVALID_INPUT = "invalid_input"
    ERROR = "error"


class FailureKind(StrEnum):
    VALUE_MISMATCH = "value_mismatch"
    UNKNOWN_VALUE = "unknown_value"
    MISSING_TRANSACTION = "missing_transaction"
    EXTRA_TRANSACTION = "extra_transaction"


class AssertionValidation(StrEnum):
    CANDIDATE = "candidate"
    VALIDATED = "validated"
    FAILED = "failed"
    UNSUPPORTED = "unsupported"


class ProvenanceKind(StrEnum):
    SYNTHETIC_FIXTURE = "synthetic_fixture"
    ANALYSIS = "analysis"


# --------------------------------------------------------------------------
# Values, signals, time
# --------------------------------------------------------------------------


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _require_unique(values: Iterable[str], what: str) -> None:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            raise ValueError(f"duplicate {what}: {value}")
        seen.add(value)


class LogicValue(ContractModel):
    """A four-state vector: exactly ``width`` characters of ``0``, ``1``, ``x``, ``z``.

    ``bits[0]`` is the declared left (most significant) bit. Uppercase input is
    normalized to lowercase. There is no implicit integer conversion.
    """

    width: PositiveInt
    bits: Bits

    @model_validator(mode="after")
    def _check_width(self) -> Self:
        if len(self.bits) != self.width:
            raise ValueError(f"bits has {len(self.bits)} characters, width is {self.width}")
        return self

    @property
    def is_fully_known(self) -> bool:
        return "x" not in self.bits and "z" not in self.bits

    def to_unsigned(self) -> int:
        """Explicit unsigned conversion; raises ``ValueError`` if any bit is x/z."""
        if not self.is_fully_known:
            raise ValueError(f"cannot convert {self.bits!r} to an integer: contains x/z")
        return int(self.bits, 2)

    @classmethod
    def from_unsigned(cls, value: int, width: int) -> LogicValue:
        if isinstance(value, bool) or value < 0 or width < 1 or value >= 1 << width:
            raise ValueError(f"{value} does not fit an unsigned {width}-bit vector")
        return cls(width=width, bits=format(value, f"0{width}b"))

    @classmethod
    def unknown(cls, width: int) -> LogicValue:
        return cls(width=width, bits="x" * width)


class BitRange(ContractModel):
    """A packed range ``[left:right]`` using RTL indices; either order is legal."""

    left: Count
    right: Count

    @property
    def width(self) -> int:
        return abs(self.left - self.right) + 1


class SignalDecl(ContractModel):
    """Declared signal metadata. ``bit_range`` is None for an unranged scalar."""

    path: SignalPath
    width: PositiveInt
    bit_range: BitRange | None = None
    signed: bool = False

    @model_validator(mode="after")
    def _check_range(self) -> Self:
        if self.bit_range is None:
            if self.width != 1:
                raise ValueError("a signal wider than one bit must declare bit_range")
        elif self.bit_range.width != self.width:
            raise ValueError("bit_range does not span exactly width bits")
        return self


class TimeInterval(ContractModel):
    """Half-open interval ``[start_ps, end_ps)``; never empty."""

    start_ps: TimePs
    end_ps: TimePs

    @model_validator(mode="after")
    def _check_order(self) -> Self:
        if self.start_ps >= self.end_ps:
            raise ValueError("interval requires start_ps < end_ps")
        return self

    def contains(self, time_ps: int) -> bool:
        return self.start_ps <= time_ps < self.end_ps


class TraceCoverage(ContractModel):
    """Inclusive time range ``[start_ps, end_ps]`` the trace claims to cover."""

    start_ps: TimePs
    end_ps: TimePs

    @model_validator(mode="after")
    def _check_order(self) -> Self:
        if self.start_ps > self.end_ps:
            raise ValueError("coverage requires start_ps <= end_ps")
        if self.end_ps >= MAX_TIME_PS:
            # Keeps the exclusive end of a whole-trace interval representable.
            raise ValueError("coverage end_ps must be less than MAX_TIME_PS")
        return self


def _check_intervals(intervals: Sequence[TimeInterval]) -> None:
    for previous, current in zip(intervals, intervals[1:]):
        if previous.end_ps >= current.start_ps:
            raise ValueError("unavailable intervals must be ascending and non-adjacent")


class SignalSample(ContractModel):
    """The settled value a signal takes from ``time_ps`` onward."""

    time_ps: TimePs
    value: LogicValue


class SignalWindow(ContractModel):
    """One signal's portion of a ``WaveformWindow``.

    ``initial`` is the settled value at the window start, or None when the start
    lies in an unavailable interval. ``samples`` lie strictly inside the window.
    """

    signal_path: SignalPath
    width: PositiveInt
    initial: LogicValue | None
    samples: tuple[SignalSample, ...] = ()


class WaveformWindow(ContractModel):
    """Sparse waveform data for ``[start_ps, end_ps)``.

    A sample states the settled value from its time until the next sample or the
    next unavailable interval. A sample located exactly at the end of an
    unavailable interval restates the value after dumping resumed; it does not by
    itself assert that the signal changed. Every other sample is a real change.
    """

    start_ps: TimePs
    end_ps: TimePs
    signals: tuple[SignalWindow, ...]
    unavailable: tuple[TimeInterval, ...] = ()

    @model_validator(mode="after")
    def _check_window(self) -> Self:
        if self.start_ps >= self.end_ps:
            raise ValueError("window requires start_ps < end_ps")
        if not self.signals:
            raise ValueError("window must contain at least one signal")
        _require_unique((s.signal_path for s in self.signals), "window signal")
        _check_intervals(self.unavailable)
        for interval in self.unavailable:
            if interval.start_ps < self.start_ps or interval.end_ps > self.end_ps:
                raise ValueError("unavailable intervals must be clipped to the window")
        start_hidden = any(i.contains(self.start_ps) for i in self.unavailable)
        resume_times = {i.end_ps for i in self.unavailable if i.end_ps < self.end_ps}
        for signal in self.signals:
            self._check_signal(signal, start_hidden, resume_times)
        return self

    def _check_signal(
        self, signal: SignalWindow, start_hidden: bool, resume_times: set[int]
    ) -> None:
        name = signal.signal_path
        if (signal.initial is None) != start_hidden:
            raise ValueError(f"{name}: initial must be null exactly when the start is unavailable")
        if signal.initial is not None and signal.initial.width != signal.width:
            raise ValueError(f"{name}: initial value width mismatch")
        previous_time = self.start_ps
        previous_value = signal.initial
        sample_times: set[int] = set()
        for sample in signal.samples:
            if not previous_time < sample.time_ps < self.end_ps:
                raise ValueError(f"{name}: samples must ascend strictly inside the window")
            if sample.value.width != signal.width:
                raise ValueError(f"{name}: sample width mismatch")
            if any(i.contains(sample.time_ps) for i in self.unavailable):
                raise ValueError(f"{name}: sample inside an unavailable interval")
            if sample.time_ps not in resume_times and sample.value == previous_value:
                raise ValueError(f"{name}: repeated value at {sample.time_ps} is not a change")
            previous_time, previous_value = sample.time_ps, sample.value
            sample_times.add(sample.time_ps)
        if not resume_times <= sample_times:
            raise ValueError(f"{name}: missing a sample where dumping resumed")


# --------------------------------------------------------------------------
# Waveform provider protocol
# --------------------------------------------------------------------------


@runtime_checkable
class WaveformProvider(Protocol):
    """Read-only access to one normalized trace. Implemented by Track A.

    All times are integer picoseconds. Errors are raised in this order: a
    malformed request (``InvalidQueryError``), an unresolved path
    (``SignalNotFoundError``), time outside coverage (``OutOfRangeError``), then
    a dump gap (``DataUnavailableError``). No method returns a placeholder value
    for a failed query.
    """

    def get_coverage(self) -> TraceCoverage:
        """Return the inclusive time range covered by the trace."""
        ...

    def get_signal_decl(self, signal_path: str) -> SignalDecl:
        """Return declared metadata, or raise ``SignalNotFoundError``."""
        ...

    def get_value_at(
        self,
        signal_path: str,
        time_ps: int,
        *,
        phase: SamplingPhase = SamplingPhase.SETTLED,
    ) -> LogicValue:
        """Return the value observed at ``time_ps`` in the given phase.

        ``settled`` returns the final recorded state at or before ``time_ps``.
        ``before`` returns the final recorded state strictly before ``time_ps``.
        If the signal has no record yet at the observed instant, the result is
        all-``x`` at the declared width; this includes ``before`` at the trace
        start. ``time_ps`` must lie inside the trace coverage. ``settled`` raises
        ``DataUnavailableError`` when ``time_ps`` is in a dump gap; ``before``
        raises it when ``time_ps - 1`` is.
        """
        ...

    def get_clock_edges(
        self,
        clock_path: str,
        start_ps: int,
        end_ps: int,
        *,
        edge: ClockEdge = ClockEdge.RISING,
    ) -> tuple[int, ...]:
        """Return ascending, duplicate-free edge times T with start_ps <= T < end_ps.

        A rising edge at T means ``before`` is known ``0`` and ``settled`` is
        known ``1``; a falling edge is the reverse. Transitions involving x/z
        are not edges. The clock must be one bit wide. Every picosecond of the
        interval must be covered, and the interval must not touch a dump gap
        (including the instant just before ``start_ps``), because edges inside a
        gap cannot be ruled out. An empty tuple means no edge occurred.
        """
        ...

    def get_window_slice(
        self, signal_paths: Sequence[str], start_ps: int, end_ps: int
    ) -> WaveformWindow:
        """Return a sparse window over ``[start_ps, end_ps)``.

        Signals appear in request order. Each carries its settled value at
        ``start_ps`` (a change exactly at ``start_ps`` is folded into it) and the
        changes strictly inside the interval. Dump gaps are returned as clipped
        unavailable intervals rather than raising. The signal list must be a
        non-empty sequence of distinct paths, not a bare string.
        """
        ...


# --------------------------------------------------------------------------
# Comparison results
# --------------------------------------------------------------------------


class Observation(ContractModel):
    """Identity of one sample: reset epoch, cycle, phase, and the edge time.

    ``cycle`` counts rising clock edges from 0 within ``epoch``. ``time_ps`` is
    the time of that clock edge and is the time to pass to ``get_value_at``.
    """

    epoch: Count
    cycle: Count
    phase: SamplingPhase
    time_ps: TimePs


class FailureAnchor(ContractModel):
    """The first divergence between the trace and the golden expectation.

    ``provider.get_value_at(signal_path, observation.time_ps,
    phase=observation.phase)`` reproduces ``actual`` whenever ``actual`` is
    present. A missing transaction has no actual payload and an extra
    transaction has no expected payload; neither is invented.
    """

    kind: FailureKind
    mode: ComparisonMode
    signal_path: SignalPath
    clock_path: SignalPath
    observation: Observation
    transaction_index: Count | None = None
    expected: LogicValue | None
    actual: LogicValue | None
    detail: Text

    @model_validator(mode="after")
    def _check_anchor(self) -> Self:
        expected, actual = self.expected, self.actual
        if self.mode is ComparisonMode.CYCLE:
            if self.transaction_index is not None:
                raise ValueError("cycle-mode failures carry no transaction_index")
            if self.kind in (FailureKind.MISSING_TRANSACTION, FailureKind.EXTRA_TRANSACTION):
                raise ValueError("transaction failures require handshake mode")
        else:
            if self.transaction_index is None:
                raise ValueError("handshake-mode failures require transaction_index")
            if self.observation.phase is not SamplingPhase.BEFORE:
                raise ValueError("handshake comparison samples in the before phase")
        if self.kind is FailureKind.MISSING_TRANSACTION:
            if expected is None or actual is not None:
                raise ValueError("missing_transaction needs expected and a null actual")
        elif self.kind is FailureKind.EXTRA_TRANSACTION:
            if expected is not None or actual is None:
                raise ValueError("extra_transaction needs actual and a null expected")
        else:
            if expected is None or actual is None:
                raise ValueError(f"{self.kind.value} needs both expected and actual")
            if expected.width != actual.width:
                raise ValueError("expected and actual widths differ")
            if self.kind is FailureKind.VALUE_MISMATCH:
                if not actual.is_fully_known:
                    raise ValueError("an actual value with x/z is unknown_value")
                if expected == actual:
                    raise ValueError("value_mismatch requires differing values")
            elif actual.is_fully_known:
                raise ValueError("unknown_value requires x/z in the actual value")
        if expected is not None and not expected.is_fully_known:
            raise ValueError("expected values must be fully known in schema 1")
        return self


class Diagnostic(ContractModel):
    code: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]*$")]
    message: Text


class ComparisonResult(ContractModel):
    """Track A's verdict for one golden trace against one waveform.

    * ``pass``: every required expectation was observed and matched.
    * ``fail``: ``failure`` is the first divergence; other mismatches at the
      same observation are kept in ``additional_mismatches``.
    * ``inconclusive``: coverage, sampling, or reset evidence was insufficient.
    * ``unsupported``: the input exceeds the declared MVP capability.
    * ``invalid_input``: the trace or golden data is malformed.
    * ``error``: tooling or infrastructure failed.

    ``compared_values`` counts expected values checked, including those at a
    failing observation. Comparing nothing is never a pass.
    """

    schema_version: Literal[1]
    status: AnalysisStatus
    mode: ComparisonMode
    failure: FailureAnchor | None
    additional_mismatches: tuple[FailureAnchor, ...] = ()
    compared_values: Count
    diagnostics: tuple[Diagnostic, ...] = ()

    @model_validator(mode="after")
    def _check_result(self) -> Self:
        if self.status is AnalysisStatus.FAIL:
            if self.failure is None:
                raise ValueError("a fail result requires a failure anchor")
            if self.failure.mode is not self.mode:
                raise ValueError("failure mode differs from the comparison mode")
            for other in self.additional_mismatches:
                if other.observation != self.failure.observation or other.mode is not self.mode:
                    raise ValueError("additional mismatches must share the failing observation")
                if other.signal_path == self.failure.signal_path:
                    raise ValueError("additional mismatch repeats the anchor signal")
            return self
        if self.failure is not None or self.additional_mismatches:
            raise ValueError(f"a {self.status.value} result carries no failure")
        if self.status is AnalysisStatus.PASS:
            if self.compared_values < 1:
                raise ValueError("a pass must have compared at least one value")
        elif not self.diagnostics:
            raise ValueError(f"a {self.status.value} result must explain itself in diagnostics")
        return self


# --------------------------------------------------------------------------
# Golden trace (JSONL)
# --------------------------------------------------------------------------


class GoldenSignal(ContractModel):
    name: Identifier
    path: SignalPath
    width: PositiveInt


class GoldenHeader(ContractModel):
    """First JSONL record. ``signals`` order is the same-observation tie-break."""

    type: Literal["metadata"]
    schema_version: Literal[1]
    mode: ComparisonMode
    clock: SignalPath
    reset: SignalPath
    reset_active: Literal["0", "1"]
    sampling: SamplingPhase
    signals: tuple[GoldenSignal, ...]
    valid: SignalPath | None = None
    ready: SignalPath | None = None
    deadline_cycle: Count | None = None

    @model_validator(mode="after")
    def _check_header(self) -> Self:
        if not self.signals:
            raise ValueError("golden metadata must declare at least one signal")
        _require_unique((s.name for s in self.signals), "golden signal name")
        _require_unique((s.path for s in self.signals), "golden signal path")
        if self.mode is ComparisonMode.HANDSHAKE:
            if self.valid is None or self.ready is None:
                raise ValueError("handshake mode requires valid and ready paths")
            if self.sampling is not SamplingPhase.BEFORE:
                raise ValueError("handshake mode samples in the before phase")
        elif self.valid is not None or self.ready is not None or self.deadline_cycle is not None:
            raise ValueError("valid, ready, and deadline_cycle apply only to handshake mode")
        return self


class GoldenRecord(ContractModel):
    """One expectation. ``values`` maps a declared name to exact-width ``01`` bits."""

    type: Literal["expected"]
    epoch: Count
    cycle_index: Count | None = None
    transaction_index: Count | None = None
    values: dict[str, str]

    @model_validator(mode="after")
    def _check_index(self) -> Self:
        if (self.cycle_index is None) == (self.transaction_index is None):
            raise ValueError("a record carries exactly one of cycle_index, transaction_index")
        return self

    @property
    def index(self) -> int:
        chosen = self.cycle_index if self.cycle_index is not None else self.transaction_index
        assert chosen is not None
        return chosen


class GoldenTrace(ContractModel):
    """A validated golden stream: one header followed by ordered records.

    Epochs start at 0 and never decrease or skip. Within an epoch the index
    starts at 0 and increases by exactly one. Cycle-mode records may omit
    signals that are not compared in that cycle; handshake records must give
    every declared signal. At least one value must be expected overall.
    """

    header: GoldenHeader
    records: tuple[GoldenRecord, ...]

    @model_validator(mode="after")
    def _check_trace(self) -> Self:
        widths = {s.name: s.width for s in self.header.signals}
        handshake = self.header.mode is ComparisonMode.HANDSHAKE
        epoch, expected_index, total = 0, 0, 0
        for position, record in enumerate(self.records):
            if (record.transaction_index is not None) != handshake:
                raise ValueError(f"record {position}: index field does not match the mode")
            if record.epoch == epoch + 1 and (position > 0 or expected_index > 0):
                epoch, expected_index = record.epoch, 0
            if record.epoch != epoch or record.index != expected_index:
                raise ValueError(
                    f"record {position}: expected epoch {epoch} index {expected_index}, "
                    f"got epoch {record.epoch} index {record.index}"
                )
            expected_index += 1
            if handshake and set(record.values) != set(widths):
                raise ValueError(f"record {position}: handshake records need every signal")
            for name, bits in record.values.items():
                if name not in widths:
                    raise ValueError(f"record {position}: undeclared signal {name!r}")
                if not _BINARY_RE.fullmatch(bits) or len(bits) != widths[name]:
                    raise ValueError(
                        f"record {position}: {name!r} must be exactly {widths[name]} "
                        "characters of 0/1"
                    )
            total += len(record.values)
        if total == 0:
            raise ValueError("a golden trace must expect at least one value")
        return self

    def to_jsonl(self) -> str:
        lines = [self.header.model_dump_json(exclude_none=True)]
        lines.extend(record.model_dump_json(exclude_none=True) for record in self.records)
        return "\n".join(lines) + "\n"


def parse_golden_jsonl(text: str) -> GoldenTrace:
    """Parse golden JSONL: a metadata line, then expected lines; no blank lines.

    Raises ``ValueError`` (including pydantic's ``ValidationError``) on any
    malformed, duplicated, skipped, or mode-inconsistent content.
    """
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if not lines:
        raise ValueError("golden JSONL is empty")
    if any(not line.strip() for line in lines):
        raise ValueError("golden JSONL must not contain blank lines")
    header = GoldenHeader.model_validate_json(lines[0])
    records = tuple(GoldenRecord.model_validate_json(line) for line in lines[1:])
    return GoldenTrace(header=header, records=records)


# --------------------------------------------------------------------------
# Causal report
# --------------------------------------------------------------------------


class SourceFile(ContractModel):
    """An analyzed source file, identified by relative path and SHA-256."""

    id: Identifier
    path: Annotated[str, AfterValidator(_check_relative_path)]
    sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class SourceSpan(ContractModel):
    """Inclusive 1-based line span inside a ``SourceFile``."""

    source_id: Identifier
    line: PositiveInt
    end_line: PositiveInt

    @model_validator(mode="after")
    def _check_lines(self) -> Self:
        if self.end_line < self.line:
            raise ValueError("end_line precedes line")
        return self


class CausalNode(ContractModel):
    """A signal (or part-select) at one observation in the time-unrolled graph.

    ``value`` is None when the signal was not observed; it is never a guess.
    """

    id: Identifier
    signal_path: SignalPath
    bit_range: BitRange | None = None
    observation: Observation
    value: LogicValue | None
    location: SourceSpan | None


class CausalEdge(ContractModel):
    """A dependence from the ``source`` node's observation to the ``target``'s.

    ``sequential``: one storage boundary at one actual clock edge. The source is
    sampled ``before`` that edge and the target is ``settled`` at the same edge.
    ``combinational``: no storage boundary. Source and target share one
    observation, or the edge is a carry of the same signal from ``settled`` at
    an earlier cycle to ``before`` at a later cycle of the same epoch, across
    which the value did not change. No edge implies a fixed clock period.
    """

    id: Identifier
    source: Identifier
    target: Identifier
    edge_type: EdgeType
    role: EdgeRole
    branch: BranchStatus
    reason: Text


class SuspectLocation(ContractModel):
    """A ranked candidate cause. Rank is ordinal; it is not a probability."""

    id: Identifier
    rank: PositiveInt
    node_id: Identifier
    location: SourceSpan | None
    reason: Text
    evidence_node_ids: tuple[Identifier, ...] = ()


class CandidateAssertion(ContractModel):
    id: Identifier
    text: Text
    validation: AssertionValidation
    tool: Text | None = None

    @model_validator(mode="after")
    def _check_tool(self) -> Self:
        checked = (AssertionValidation.VALIDATED, AssertionValidation.FAILED)
        if self.validation in checked and self.tool is None:
            raise ValueError("a validated or failed assertion must name its tool")
        return self


class Provenance(ContractModel):
    kind: ProvenanceKind
    case_id: Identifier | None = None
    note: Text


def _carry_ok(source: CausalNode, target: CausalNode) -> bool:
    s, t = source.observation, target.observation
    return (
        source.signal_path == target.signal_path
        and source.bit_range == target.bit_range
        and s.epoch == t.epoch
        and s.phase is SamplingPhase.SETTLED
        and t.phase is SamplingPhase.BEFORE
        and s.cycle < t.cycle
        and (source.value is None or target.value is None or source.value == target.value)
    )


class CausalReport(ContractModel):
    """Track B's output and Track C's input.

    ``pass`` carries no failure and no graph. ``fail`` carries the failure; the
    graph may be empty when localization was not possible, which diagnostics must
    explain. Every other status carries no failure and must be explained.
    ``truncated`` means the backward slice stopped at a depth, time, or
    capability limit, so absent nodes are not evidence of irrelevance.
    """

    schema_version: Literal[1]
    provenance: Provenance
    status: AnalysisStatus
    failure: FailureAnchor | None
    anchor_node_id: Identifier | None = None
    nodes: tuple[CausalNode, ...] = ()
    edges: tuple[CausalEdge, ...] = ()
    suspects: tuple[SuspectLocation, ...] = ()
    sources: tuple[SourceFile, ...] = ()
    assertions: tuple[CandidateAssertion, ...] = ()
    waveform: WaveformWindow | None = None
    truncated: bool = False
    diagnostics: tuple[Diagnostic, ...] = ()

    @model_validator(mode="after")
    def _check_report(self) -> Self:
        self._check_status()
        nodes = {node.id: node for node in self.nodes}
        _require_unique((n.id for n in self.nodes), "node id")
        _require_unique((e.id for e in self.edges), "edge id")
        _require_unique((s.id for s in self.suspects), "suspect id")
        _require_unique((s.id for s in self.sources), "source id")
        _require_unique((a.id for a in self.assertions), "assertion id")
        source_ids = {source.id for source in self.sources}
        self._check_nodes(nodes, source_ids)
        for edge in self.edges:
            self._check_edge(edge, nodes)
        self._check_suspects(nodes, source_ids)
        return self

    def _check_status(self) -> None:
        graph = bool(self.nodes or self.edges or self.suspects or self.assertions)
        if self.status is AnalysisStatus.FAIL:
            if self.failure is None:
                raise ValueError("a fail report requires a failure anchor")
            if not self.nodes and not self.diagnostics:
                raise ValueError("an unlocalized failure must be explained in diagnostics")
        else:
            if self.failure is not None or graph or self.anchor_node_id is not None:
                raise ValueError(f"a {self.status.value} report carries no failure or graph")
            if self.status is not AnalysisStatus.PASS and not self.diagnostics:
                raise ValueError(f"a {self.status.value} report must explain itself")
        if self.truncated and (self.status is not AnalysisStatus.FAIL or not self.diagnostics):
            raise ValueError("truncated applies to a fail report and needs a diagnostic")

    def _check_nodes(self, nodes: dict[str, CausalNode], source_ids: set[str]) -> None:
        edge_times: dict[tuple[int, int], int] = {}
        for node in self.nodes:
            obs = node.observation
            known = edge_times.setdefault((obs.epoch, obs.cycle), obs.time_ps)
            if known != obs.time_ps:
                raise ValueError(f"node {node.id}: cycle maps to two different edge times")
            if node.location is not None and node.location.source_id not in source_ids:
                raise ValueError(f"node {node.id}: unknown source {node.location.source_id}")
            if node.bit_range is not None and node.value is not None:
                if node.bit_range.width != node.value.width:
                    raise ValueError(f"node {node.id}: value width differs from bit_range")
        if not self.nodes:
            if self.edges or self.suspects:
                raise ValueError("edges and suspects require nodes")
            return
        failure = self.failure
        anchor = nodes.get(self.anchor_node_id or "")
        if failure is None or anchor is None:
            raise ValueError("a graph requires anchor_node_id naming an existing node")
        if anchor.signal_path != failure.signal_path or anchor.observation != failure.observation:
            raise ValueError("anchor node does not match the failure signal and observation")
        if anchor.value != failure.actual:
            raise ValueError("anchor node value differs from the failure's actual value")

    @staticmethod
    def _check_edge(edge: CausalEdge, nodes: dict[str, CausalNode]) -> None:
        source, target = nodes.get(edge.source), nodes.get(edge.target)
        if source is None or target is None:
            raise ValueError(f"edge {edge.id}: dangling node reference")
        if source is target:
            raise ValueError(f"edge {edge.id}: self-loop")
        s, t = source.observation, target.observation
        if edge.edge_type is EdgeType.SEQUENTIAL:
            same_edge = (s.epoch, s.cycle, s.time_ps) == (t.epoch, t.cycle, t.time_ps)
            crosses = s.phase is SamplingPhase.BEFORE and t.phase is SamplingPhase.SETTLED
            if not (same_edge and crosses):
                raise ValueError(
                    f"edge {edge.id}: a sequential edge runs from before to settled "
                    "at one clock edge"
                )
        elif s != t and not _carry_ok(source, target):
            raise ValueError(
                f"edge {edge.id}: a combinational edge stays in one observation or "
                "carries one unchanged signal from settled to a later before"
            )

    def _check_suspects(self, nodes: dict[str, CausalNode], source_ids: set[str]) -> None:
        ranks = sorted(suspect.rank for suspect in self.suspects)
        if ranks != list(range(1, len(ranks) + 1)):
            raise ValueError("suspect ranks must be exactly 1..N without ties")
        for suspect in self.suspects:
            referenced = (suspect.node_id, *suspect.evidence_node_ids)
            if any(node_id not in nodes for node_id in referenced):
                raise ValueError(f"suspect {suspect.id}: dangling node reference")
            if suspect.location is not None and suspect.location.source_id not in source_ids:
                raise ValueError(f"suspect {suspect.id}: unknown source")


# --------------------------------------------------------------------------
# Mock trace, fixture, and provider
# --------------------------------------------------------------------------


class MockSignal(ContractModel):
    decl: SignalDecl
    samples: tuple[SignalSample, ...] = ()


class MockTrace(ContractModel):
    """Hand-authored trace data with the same invariants as a normalized trace.

    One final value per signal per timestamp. Samples ascend strictly, lie in
    coverage and outside dump gaps, and record real changes, except that every
    signal restates its value exactly where a dump gap ends. A signal is all-x
    before its first sample, so an all-x first sample is not a change; it is
    allowed only at the coverage start or where a dump gap ends.
    """

    coverage: TraceCoverage
    unavailable: tuple[TimeInterval, ...] = ()
    signals: tuple[MockSignal, ...]

    @model_validator(mode="after")
    def _check_trace(self) -> Self:
        if not self.signals:
            raise ValueError("a trace needs at least one signal")
        _require_unique((s.decl.path for s in self.signals), "signal path")
        _check_intervals(self.unavailable)
        start, end = self.coverage.start_ps, self.coverage.end_ps
        for interval in self.unavailable:
            if interval.start_ps < start or interval.end_ps > end + 1:
                raise ValueError("unavailable interval lies outside coverage")
        resume_times = {i.end_ps for i in self.unavailable if i.end_ps <= end}
        for signal in self.signals:
            name = signal.decl.path
            previous: SignalSample | None = None
            for sample in signal.samples:
                if not start <= sample.time_ps <= end:
                    raise ValueError(f"{name}: sample outside coverage")
                if sample.value.width != signal.decl.width:
                    raise ValueError(f"{name}: sample width differs from declaration")
                if any(i.contains(sample.time_ps) for i in self.unavailable):
                    raise ValueError(f"{name}: sample inside an unavailable interval")
                if previous is None:
                    all_x = set(sample.value.bits) == {"x"}
                    if all_x and sample.time_ps != start and sample.time_ps not in resume_times:
                        raise ValueError(
                            f"{name}: an all-x first sample at {sample.time_ps} is not a change"
                        )
                else:
                    if sample.time_ps <= previous.time_ps:
                        raise ValueError(f"{name}: sample times must ascend strictly")
                    repeated = sample.value == previous.value
                    if repeated and sample.time_ps not in resume_times:
                        raise ValueError(f"{name}: repeated value at {sample.time_ps}")
                previous = sample
            if not resume_times <= {sample.time_ps for sample in signal.samples}:
                raise ValueError(f"{name}: missing a sample where dumping resumed")
        return self


class MockFixture(ContractModel):
    """A complete synthetic scenario for cross-track development.

    ``comparison`` and ``report`` are hand-authored fixture data. They are not
    the output of a working comparison engine or causal engine.
    """

    schema_version: Literal[1]
    provenance: Provenance
    trace: MockTrace
    golden: GoldenTrace
    comparison: ComparisonResult
    report: CausalReport

    @model_validator(mode="after")
    def _check_fixture(self) -> Self:
        kinds = (self.provenance.kind, self.report.provenance.kind)
        if any(kind is not ProvenanceKind.SYNTHETIC_FIXTURE for kind in kinds):
            raise ValueError("a mock fixture must be labeled synthetic_fixture")
        if self.comparison.failure != self.report.failure:
            raise ValueError("comparison and report disagree about the failure")
        return self


def load_mock_fixture(path: Path) -> MockFixture:
    """Load and validate a mock fixture JSON file."""
    return MockFixture.model_validate_json(path.read_text(encoding="utf-8"))


def _check_time(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InvalidQueryError(f"{name} must be a non-negative integer picosecond count")
    return value


class MockWaveformProvider:
    """A ``WaveformProvider`` over a ``MockTrace``.

    It follows the query, sampling, range, and error semantics required of the
    real provider, using binary search over each signal's sample times.
    """

    def __init__(self, trace: MockTrace) -> None:
        self._trace = trace
        self._signals = {signal.decl.path: signal for signal in trace.signals}
        self._times = {
            path: [sample.time_ps for sample in signal.samples]
            for path, signal in self._signals.items()
        }
        self._resume_times = frozenset(i.end_ps for i in trace.unavailable)

    def get_coverage(self) -> TraceCoverage:
        return self._trace.coverage

    def get_signal_decl(self, signal_path: str) -> SignalDecl:
        return self._signal(signal_path).decl

    def get_value_at(
        self,
        signal_path: str,
        time_ps: int,
        *,
        phase: SamplingPhase = SamplingPhase.SETTLED,
    ) -> LogicValue:
        time_ps = _check_time(time_ps, "time_ps")
        if not isinstance(phase, SamplingPhase):
            raise InvalidQueryError("phase must be a SamplingPhase")
        signal = self._signal(signal_path)
        coverage = self._trace.coverage
        if not coverage.start_ps <= time_ps <= coverage.end_ps:
            raise OutOfRangeError(
                f"{time_ps} ps is outside trace coverage "
                f"[{coverage.start_ps}, {coverage.end_ps}]"
            )
        if phase is SamplingPhase.SETTLED:
            instant = time_ps
        elif time_ps == coverage.start_ps:
            return LogicValue.unknown(signal.decl.width)
        else:
            instant = time_ps - 1
        self._require_available(instant)
        return self._state_at(signal, instant)

    def get_clock_edges(
        self,
        clock_path: str,
        start_ps: int,
        end_ps: int,
        *,
        edge: ClockEdge = ClockEdge.RISING,
    ) -> tuple[int, ...]:
        start_ps, end_ps = self._check_interval(start_ps, end_ps)
        if not isinstance(edge, ClockEdge):
            raise InvalidQueryError("edge must be a ClockEdge")
        signal = self._signal(clock_path)
        if signal.decl.width != 1:
            raise InvalidQueryError(f"{clock_path} is {signal.decl.width} bits; a clock is 1 bit")
        self._require_covered(start_ps, end_ps)
        first_instant = max(start_ps - 1, self._trace.coverage.start_ps)
        for interval in self._trace.unavailable:
            if interval.start_ps < end_ps and interval.end_ps > first_instant:
                raise DataUnavailableError(
                    f"clock edges cannot be established across the dump gap "
                    f"[{interval.start_ps}, {interval.end_ps})"
                )
        low, high = ("0", "1") if edge is ClockEdge.RISING else ("1", "0")
        times = self._times[clock_path]
        edges: list[int] = []
        for position in range(bisect.bisect_left(times, start_ps), len(times)):
            time_ps = times[position]
            if time_ps >= end_ps:
                break
            previous = signal.samples[position - 1].value.bits if position else "x"
            if previous == low and signal.samples[position].value.bits == high:
                edges.append(time_ps)
        return tuple(edges)

    def get_window_slice(
        self, signal_paths: Sequence[str], start_ps: int, end_ps: int
    ) -> WaveformWindow:
        start_ps, end_ps = self._check_interval(start_ps, end_ps)
        if isinstance(signal_paths, str) or not signal_paths:
            raise InvalidQueryError("signal_paths must be a non-empty sequence of paths")
        if len(set(signal_paths)) != len(signal_paths):
            raise InvalidQueryError("signal_paths must not contain duplicates")
        signals = [self._signal(path) for path in signal_paths]
        self._require_covered(start_ps, end_ps)
        unavailable = tuple(
            TimeInterval(
                start_ps=max(interval.start_ps, start_ps),
                end_ps=min(interval.end_ps, end_ps),
            )
            for interval in self._trace.unavailable
            if interval.start_ps < end_ps and interval.end_ps > start_ps
        )
        start_hidden = any(interval.contains(start_ps) for interval in unavailable)
        windows: list[SignalWindow] = []
        for signal in signals:
            initial = None if start_hidden else self._state_at(signal, start_ps)
            times = self._times[signal.decl.path]
            first = bisect.bisect_right(times, start_ps)
            last = bisect.bisect_left(times, end_ps)
            windows.append(
                SignalWindow(
                    signal_path=signal.decl.path,
                    width=signal.decl.width,
                    initial=initial,
                    samples=signal.samples[first:last],
                )
            )
        return WaveformWindow(
            start_ps=start_ps, end_ps=end_ps, signals=tuple(windows), unavailable=unavailable
        )

    def _signal(self, signal_path: str) -> MockSignal:
        signal = self._signals.get(signal_path) if isinstance(signal_path, str) else None
        if signal is None:
            raise SignalNotFoundError(f"signal {signal_path!r} is not present in the trace")
        return signal

    @staticmethod
    def _check_interval(start_ps: int, end_ps: int) -> tuple[int, int]:
        start_ps = _check_time(start_ps, "start_ps")
        end_ps = _check_time(end_ps, "end_ps")
        if start_ps >= end_ps:
            raise InvalidQueryError("an interval requires start_ps < end_ps")
        return start_ps, end_ps

    def _require_covered(self, start_ps: int, end_ps: int) -> None:
        coverage = self._trace.coverage
        if start_ps < coverage.start_ps or end_ps - 1 > coverage.end_ps:
            raise OutOfRangeError(
                f"[{start_ps}, {end_ps}) is not inside trace coverage "
                f"[{coverage.start_ps}, {coverage.end_ps}]"
            )

    def _require_available(self, instant: int) -> None:
        for interval in self._trace.unavailable:
            if interval.contains(instant):
                raise DataUnavailableError(
                    f"{instant} ps lies in the dump gap [{interval.start_ps}, {interval.end_ps})"
                )

    def _state_at(self, signal: MockSignal, instant: int) -> LogicValue:
        position = bisect.bisect_right(self._times[signal.decl.path], instant)
        if position == 0:
            return LogicValue.unknown(signal.decl.width)
        return signal.samples[position - 1].value

