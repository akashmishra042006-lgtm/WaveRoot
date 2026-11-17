"""VCD reading and the time-indexed ``WaveformProvider`` for Track A.

``parse_vcd`` turns VCD text into a ``ParsedTrace``: per-signal change lists in
exact integer picoseconds, declared metadata, trace coverage, and dump gaps.
``VcdWaveformProvider`` answers the queries defined by
``src.contracts.WaveformProvider`` with binary search over those lists.

Normalization rules follow ``spec.md`` sections 3, 4, 5, 6.1, and 9:

* One final value per signal per timestamp. Record order inside a timestamp
  carries no meaning, but a signal that changes more than once at one timestamp
  is remembered so a clock query can refuse it.
* Values keep ``x``/``z``. Short known vectors are zero-extended; a leading
  ``x`` or ``z`` is extended with itself. Over-width values are rejected.
* ``$dumpoff`` at time a to ``$dumpon`` at time b makes ``[a, b)`` unavailable.
  Every signal must be restated at b.
* Nothing is rounded, guessed, or silently dropped. Variables this schema cannot
  represent are listed in ``ParsedTrace.skipped`` and cannot be queried.
"""

from __future__ import annotations

import bisect
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from src.contracts import (
    BitRange,
    ClockEdge,
    ContractError,
    DataUnavailableError,
    IndeterminateClockError,
    InvalidQueryError,
    LogicValue,
    OutOfRangeError,
    SamplingPhase,
    SignalDecl,
    SignalNotFoundError,
    SignalSample,
    SignalWindow,
    TimeInterval,
    TimeUnit,
    TraceCoverage,
    WaveformWindow,
    to_picoseconds,
)
from src.waveform.select import check_patterns, match_paths


class InvalidTraceError(ContractError):
    """The VCD text is malformed, truncated, or internally inconsistent."""


class UnsupportedTraceError(ContractError):
    """The VCD is well formed but outside what schema 1 can represent."""


_COMPONENT: Final = r"[A-Za-z_][A-Za-z0-9_$]*(?:\[[0-9]+\])*"
_COMPONENT_RE: Final = re.compile(_COMPONENT)
_REFERENCE_RE: Final = re.compile(
    r"(?P<name>[^\[\]]+)(?P<indices>(?:\[-?[0-9]+\])*)(?:\[(?P<left>-?[0-9]+):(?P<right>-?[0-9]+)\])?"
)
_TIMESCALE_RE: Final = re.compile(r"(1|10|100)(s|ms|us|ns|ps|fs)")
_VECTOR_RE: Final = re.compile(r"[01xz]+")
_UNSUPPORTED_VAR_TYPES: Final = frozenset({"real", "realtime", "event", "string"})
_BODY_BLOCKS: Final = frozenset({"$dumpvars", "$dumpall", "$dumpon", "$dumpoff"})


@dataclass(frozen=True, slots=True)
class SkippedVariable:
    """A declared variable that schema 1 cannot represent, and why."""

    scope: str
    reference: str
    reason: str


@dataclass(slots=True)
class _Series:
    """Change history for one identifier code."""

    width: int
    times: list[int] = field(default_factory=list)
    values: list[str] = field(default_factory=list)
    multi_change_times: set[int] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class ParsedTrace:
    """A normalized trace. Treat it as read-only."""

    timescale_magnitude: int
    timescale_unit: TimeUnit
    coverage: TraceCoverage
    unavailable: tuple[TimeInterval, ...]
    decls: dict[str, SignalDecl]
    skipped: tuple[SkippedVariable, ...]
    _series_by_path: dict[str, _Series]

    @property
    def signal_paths(self) -> tuple[str, ...]:
        """Every queryable path, in declaration order."""
        return tuple(self.decls)


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


class _Parser:
    def __init__(self, text: str, keep: Sequence[str] | None = None) -> None:
        self._keep = None if keep is None else check_patterns(keep)
        self._tokens = text.split()
        self._position = 0
        self._scopes: list[str] = []
        self._scope_ok: list[bool] = []
        self._magnitude: int | None = None
        self._unit: TimeUnit | None = None
        self._series: dict[str, _Series] = {}
        self._ignored_codes: set[str] = set()
        # Widths of identifier codes left out by ``keep``: validated, not stored.
        self._dropped_widths: dict[str, int] = {}
        self._code_by_path: dict[str, str] = {}
        self._decls: dict[str, SignalDecl] = {}
        self._skipped: list[SkippedVariable] = []

    # -- token helpers ----------------------------------------------------

    def _next(self, context: str) -> str:
        if self._position >= len(self._tokens):
            raise InvalidTraceError(f"trace ends unexpectedly inside {context}")
        token = self._tokens[self._position]
        self._position += 1
        return token

    def _until_end(self, context: str) -> list[str]:
        collected: list[str] = []
        while True:
            token = self._next(context)
            if token == "$end":
                return collected
            collected.append(token)

    # -- header -----------------------------------------------------------

    def parse(self) -> ParsedTrace:
        self._parse_header()
        if self._magnitude is None or self._unit is None:
            raise InvalidTraceError("trace has no $timescale")
        if self._keep is not None:
            self._apply_keep(self._keep)
        coverage, unavailable = self._parse_body(self._magnitude, self._unit)
        series_by_path = {path: self._series[code] for path, code in self._code_by_path.items()}
        return ParsedTrace(
            timescale_magnitude=self._magnitude,
            timescale_unit=self._unit,
            coverage=coverage,
            unavailable=unavailable,
            decls=self._decls,
            skipped=tuple(self._skipped),
            _series_by_path=series_by_path,
        )

    def _parse_header(self) -> None:
        while True:
            token = self._next("the declaration section (missing $enddefinitions)")
            if token == "$enddefinitions":
                self._until_end("$enddefinitions")
                if self._scopes:
                    raise InvalidTraceError("$enddefinitions reached with an open $scope")
                return
            if token == "$timescale":
                self._parse_timescale(self._until_end("$timescale"))
            elif token == "$scope":
                self._parse_scope(self._until_end("$scope"))
            elif token == "$upscope":
                self._until_end("$upscope")
                if not self._scopes:
                    raise InvalidTraceError("$upscope without a matching $scope")
                self._scopes.pop()
                self._scope_ok.pop()
            elif token == "$var":
                self._parse_var(self._until_end("$var"))
            elif token.startswith("$"):
                self._until_end(token)
            else:
                raise InvalidTraceError(f"unexpected token {token!r} in the declaration section")

    def _parse_timescale(self, body: list[str]) -> None:
        match = _TIMESCALE_RE.fullmatch("".join(body))
        if match is None:
            raise InvalidTraceError(f"invalid $timescale {' '.join(body)!r}")
        if self._magnitude is not None:
            raise InvalidTraceError("trace declares $timescale more than once")
        self._magnitude = int(match.group(1))
        self._unit = TimeUnit(match.group(2))

    def _parse_scope(self, body: list[str]) -> None:
        if len(body) != 2:
            raise InvalidTraceError(f"invalid $scope {' '.join(body)!r}")
        name = body[1]
        self._scopes.append(name)
        self._scope_ok.append(_COMPONENT_RE.fullmatch(name) is not None)

    def _parse_var(self, body: list[str]) -> None:
        if len(body) < 4:
            raise InvalidTraceError(f"invalid $var {' '.join(body)!r}")
        var_type, size_text, code = body[0], body[1], body[2]
        reference = "".join(body[3:])
        scope = ".".join(self._scopes)
        if not size_text.isdigit() or int(size_text) < 1:
            raise InvalidTraceError(f"invalid size {size_text!r} for $var {reference!r}")
        size = int(size_text)

        def skip(reason: str) -> None:
            self._skipped.append(SkippedVariable(scope=scope, reference=reference, reason=reason))
            if code not in self._series:
                self._ignored_codes.add(code)

        if var_type in _UNSUPPORTED_VAR_TYPES:
            return skip(f"variable type {var_type!r} is not a digital four-state signal")
        if not self._scopes or not all(self._scope_ok):
            return skip("enclosing scope name is not a simple identifier")
        match = _REFERENCE_RE.fullmatch(reference)
        if match is None or _COMPONENT_RE.fullmatch(match.group("name")) is None:
            return skip("name is not a simple identifier (escaped identifiers are unsupported)")
        indices = match.group("indices")
        if "-" in indices:
            return skip("negative array index")
        bit_range: BitRange | None
        if match.group("left") is not None:
            left, right = int(match.group("left")), int(match.group("right"))
            if left < 0 or right < 0:
                return skip("negative packed range bound")
            bit_range = BitRange(left=left, right=right)
            if bit_range.width != size:
                raise InvalidTraceError(
                    f"$var {reference!r} declares size {size} but range spans {bit_range.width}"
                )
        else:
            # A vector declared without a range (for example `integer`) is
            # given the conventional descending range so positions stay defined.
            bit_range = None if size == 1 else BitRange(left=size - 1, right=0)
        path = f"{scope}.{match.group('name')}{indices}"

        existing = self._series.get(code)
        if existing is not None and existing.width != size:
            raise InvalidTraceError(f"identifier code {code!r} is declared with two widths")
        if path in self._code_by_path:
            if self._code_by_path[path] != code:
                raise InvalidTraceError(f"signal path {path!r} is declared twice")
            return
        if existing is None:
            self._series[code] = _Series(width=size)
            self._ignored_codes.discard(code)
        self._code_by_path[path] = code
        self._decls[path] = SignalDecl(path=path, width=size, bit_range=bit_range)

    def _apply_keep(self, keep: tuple[str, ...]) -> None:
        kept = set(match_paths(tuple(self._decls), keep))
        kept_codes = {self._code_by_path[path] for path in kept}
        for code in [code for code in self._series if code not in kept_codes]:
            self._dropped_widths[code] = self._series.pop(code).width
        self._code_by_path = {p: c for p, c in self._code_by_path.items() if p in kept}
        self._decls = {p: d for p, d in self._decls.items() if p in kept}

    # -- body -------------------------------------------------------------

    def _parse_body(
        self, magnitude: int, unit: TimeUnit
    ) -> tuple[TraceCoverage, tuple[TimeInterval, ...]]:
        series = self._series
        ignored = self._ignored_codes
        dropped = self._dropped_widths
        tokens = self._tokens
        count = len(tokens)
        position = self._position

        time: int | None = None
        first_time: int | None = None
        pending: dict[str, list[str]] = {}
        dumping = True
        gap_start = 0
        resume_here = False
        gaps: list[tuple[int, int]] = []

        def flush() -> None:
            nonlocal resume_here
            assert time is not None
            if resume_here:
                missing = sorted(code for code in series if code not in pending)
                if missing:
                    raise InvalidTraceError(
                        f"$dumpon at {time} ps did not restate {len(missing)} signal(s)"
                    )
            for code, values in pending.items():
                entry = series[code]
                # Before its first record a signal is all-x, so an explicit
                # all-x first record is not a change.
                before = entry.values[-1] if entry.values else "x" * entry.width
                current = before
                transitions = 0
                for value in values:
                    if value != current:
                        transitions += 1
                        current = value
                if transitions > 1:
                    entry.multi_change_times.add(time)
                if current != before or resume_here:
                    entry.times.append(time)
                    entry.values.append(current)
            pending.clear()
            resume_here = False

        def record(code: str, value: str, scalar: bool) -> None:
            nonlocal time, first_time
            entry = series.get(code)
            if entry is None:
                if code in ignored:
                    return
                if code not in dropped:
                    raise InvalidTraceError(f"value change for undeclared identifier code {code!r}")
            if not dumping:
                return
            width = entry.width if entry is not None else dropped[code]
            if scalar:
                if width != 1:
                    raise InvalidTraceError(
                        f"scalar value change for {width}-bit identifier code {code!r}"
                    )
            elif len(value) != width:
                if len(value) > width:
                    raise InvalidTraceError(
                        f"value {value!r} is wider than {width}-bit identifier code {code!r}"
                    )
                fill = value[0] if value[0] in "xz" else "0"
                value = fill * (width - len(value)) + value
            if time is None:
                time = first_time = 0
            if entry is not None:
                pending.setdefault(code, []).append(value)

        while position < count:
            token = tokens[position]
            position += 1
            lead = token[0]
            if lead == "#":
                digits = token[1:]
                if not digits.isdigit():
                    raise InvalidTraceError(f"invalid timestamp {token!r}")
                new_time = to_picoseconds(int(digits), magnitude, unit)
                if time is None:
                    time = first_time = new_time
                elif new_time < time:
                    raise InvalidTraceError(f"timestamp {new_time} ps follows {time} ps")
                elif new_time > time:
                    if dumping:
                        flush()
                    time = new_time
            elif lead in "01xXzZ":
                code = token[1:]
                if not code:
                    raise InvalidTraceError(f"scalar value change {token!r} has no identifier code")
                record(code, lead.lower(), True)
            elif lead in "bB":
                value = token[1:].lower()
                if _VECTOR_RE.fullmatch(value) is None or position >= count:
                    raise InvalidTraceError(f"invalid vector value change {token!r}")
                record(tokens[position], value, False)
                position += 1
            elif lead in "rRsS":
                if position >= count:
                    raise InvalidTraceError(f"value change {token!r} has no identifier code")
                code = tokens[position]
                position += 1
                if code not in ignored and code not in series and code not in dropped:
                    raise InvalidTraceError(f"value change for undeclared identifier code {code!r}")
                if code in series or code in dropped:
                    raise InvalidTraceError(f"non-digital value for digital identifier code {code!r}")
            elif token == "$end":
                continue
            elif token in _BODY_BLOCKS:
                if time is None:
                    time = first_time = 0
                if token == "$dumpoff" and dumping:
                    pending.clear()
                    resume_here = False
                    dumping = False
                    gap_start = time
                elif token == "$dumpon" and not dumping:
                    dumping = True
                    if time > gap_start:
                        gaps.append((gap_start, time))
                        resume_here = True
            elif token == "$comment":
                while position < count and tokens[position] != "$end":
                    position += 1
                if position >= count:
                    raise InvalidTraceError("trace ends unexpectedly inside $comment")
                position += 1
            else:
                raise InvalidTraceError(f"unexpected token {token!r} in the value-change section")

        if time is None or first_time is None:
            raise InvalidTraceError("trace contains no simulation time")
        if dumping:
            flush()
        else:
            gaps.append((gap_start, time + 1))

        merged: list[tuple[int, int]] = []
        for start, end in gaps:
            if merged and merged[-1][1] >= start:
                merged[-1] = (merged[-1][0], end)
            else:
                merged.append((start, end))
        unavailable = tuple(TimeInterval(start_ps=start, end_ps=end) for start, end in merged)
        return TraceCoverage(start_ps=first_time, end_ps=time), unavailable


def parse_vcd(text: str, *, keep: Sequence[str] | None = None) -> ParsedTrace:
    """Parse VCD text into a normalized trace.

    With ``keep``, only paths matching one of the hierarchy patterns (see
    ``src.waveform.select``) are stored and queryable. Value changes for other
    identifier codes are still validated but not stored; an identifier code
    shared with a kept path is kept. Coverage and dump gaps are unaffected, and
    the ``$dumpon`` restate check applies to kept signals only.

    Raises ``InvalidTraceError`` for malformed or inconsistent input,
    ``UnsupportedPrecisionError`` when a timestamp is not a whole picosecond,
    and ``InvalidQueryError`` for a malformed or unmatched ``keep`` pattern.
    """
    return _Parser(text, keep).parse()


# --------------------------------------------------------------------------
# Provider
# --------------------------------------------------------------------------


def _check_time(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InvalidQueryError(f"{name} must be a non-negative integer picosecond count")
    return value


class VcdWaveformProvider:
    """``WaveformProvider`` over a ``ParsedTrace``.

    Point lookups cost O(log changes of that signal). Windows and edge queries
    add only the changes they return. No per-clock-tick expansion is performed.
    """

    def __init__(self, trace: ParsedTrace) -> None:
        self._trace = trace
        self._series = trace._series_by_path
        self._gap_starts = [interval.start_ps for interval in trace.unavailable]
        self._values: dict[tuple[int, str], LogicValue] = {}

    @classmethod
    def from_text(cls, text: str) -> VcdWaveformProvider:
        return cls(parse_vcd(text))

    @classmethod
    def from_path(cls, path: Path) -> VcdWaveformProvider:
        return cls(parse_vcd(path.read_text(encoding="utf-8")))

    @property
    def trace(self) -> ParsedTrace:
        return self._trace

    def get_coverage(self) -> TraceCoverage:
        return self._trace.coverage

    def get_signal_decl(self, signal_path: str) -> SignalDecl:
        self._entry(signal_path)
        return self._trace.decls[signal_path]

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
        entry = self._entry(signal_path)
        coverage = self._trace.coverage
        if not coverage.start_ps <= time_ps <= coverage.end_ps:
            raise OutOfRangeError(
                f"{time_ps} ps is outside trace coverage "
                f"[{coverage.start_ps}, {coverage.end_ps}]"
            )
        if phase is SamplingPhase.SETTLED:
            instant = time_ps
        elif time_ps == coverage.start_ps:
            return self._value(entry.width, "x" * entry.width)
        else:
            instant = time_ps - 1
        gap = self._gap_at(instant)
        if gap is not None:
            raise DataUnavailableError(
                f"{instant} ps lies in the dump gap [{gap.start_ps}, {gap.end_ps})"
            )
        return self._state_at(entry, instant)

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
        entry = self._entry(clock_path)
        if entry.width != 1:
            raise InvalidQueryError(f"{clock_path} is {entry.width} bits; a clock is 1 bit")
        self._require_covered(start_ps, end_ps)
        first_instant = max(start_ps - 1, self._trace.coverage.start_ps)
        for interval in self._trace.unavailable:
            if interval.start_ps < end_ps and interval.end_ps > first_instant:
                raise DataUnavailableError(
                    "clock edges cannot be established across the dump gap "
                    f"[{interval.start_ps}, {interval.end_ps})"
                )
        ambiguous = sorted(t for t in entry.multi_change_times if start_ps <= t < end_ps)
        if ambiguous:
            raise IndeterminateClockError(
                f"{clock_path} changes more than once at {ambiguous[0]} ps"
            )
        low, high = ("0", "1") if edge is ClockEdge.RISING else ("1", "0")
        times, values = entry.times, entry.values
        edges: list[int] = []
        for position in range(bisect.bisect_left(times, start_ps), len(times)):
            time_ps = times[position]
            if time_ps >= end_ps:
                break
            previous = values[position - 1] if position else "x"
            if previous == low and values[position] == high:
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
        entries = [(path, self._entry(path)) for path in signal_paths]
        self._require_covered(start_ps, end_ps)
        unavailable = tuple(
            TimeInterval(
                start_ps=max(interval.start_ps, start_ps),
                end_ps=min(interval.end_ps, end_ps),
            )
            for interval in self._trace.unavailable
            if interval.start_ps < end_ps and interval.end_ps > start_ps
        )
        start_hidden = self._gap_at(start_ps) is not None
        windows: list[SignalWindow] = []
        for path, entry in entries:
            first = bisect.bisect_right(entry.times, start_ps)
            last = bisect.bisect_left(entry.times, end_ps)
            windows.append(
                SignalWindow(
                    signal_path=path,
                    width=entry.width,
                    initial=None if start_hidden else self._state_at(entry, start_ps),
                    samples=tuple(
                        SignalSample(
                            time_ps=entry.times[index],
                            value=self._value(entry.width, entry.values[index]),
                        )
                        for index in range(first, last)
                    ),
                )
            )
        return WaveformWindow(
            start_ps=start_ps, end_ps=end_ps, signals=tuple(windows), unavailable=unavailable
        )

    # -- internals --------------------------------------------------------

    def _entry(self, signal_path: str) -> _Series:
        entry = self._series.get(signal_path) if isinstance(signal_path, str) else None
        if entry is None:
            raise SignalNotFoundError(f"signal {signal_path!r} is not present in the trace")
        return entry

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

    def _gap_at(self, instant: int) -> TimeInterval | None:
        position = bisect.bisect_right(self._gap_starts, instant) - 1
        if position >= 0 and self._trace.unavailable[position].contains(instant):
            return self._trace.unavailable[position]
        return None

    def _state_at(self, entry: _Series, instant: int) -> LogicValue:
        position = bisect.bisect_right(entry.times, instant)
        bits = entry.values[position - 1] if position else "x" * entry.width
        return self._value(entry.width, bits)

    def _value(self, width: int, bits: str) -> LogicValue:
        key = (width, bits)
        cached = self._values.get(key)
        if cached is None:
            cached = self._values[key] = LogicValue(width=width, bits=bits)
        return cached
