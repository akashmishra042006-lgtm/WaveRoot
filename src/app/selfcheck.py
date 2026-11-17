"""Benchmark self-check: compares golden JSONL with testbench-sampled values.

This is Track C's independent check for its own benchmarks, required by the
review gates. It is **not** Track A's comparison engine and must never be
presented as waveform analysis: it reads the phase-sampled observations that
the benchmark testbench writes (``observed.jsonl``), not the VCD. It produces
canonical ``ComparisonResult`` objects so the rest of the pipeline is
unchanged when Track A's provider is swapped in.

Rules implemented (spec.md; the stricter reset rules follow PR #8 rulings and
are compatible with the merged text):

* Epochs and cycles from pre-edge reset (section 7). Comparison runs in time
  order: an ``x``/``z`` reset stops it as ``inconclusive`` (``RESET_UNKNOWN``)
  only if no verdict was reached before that edge. An ``x``/``z`` reset before
  reset is first asserted is ignored, as in Track A's engine.
* Expectations of an epoch that a reset ends before they are observed are
  cancelled but not waived: ``inconclusive`` (``EPOCH_ENDED_EARLY`` in cycle
  mode, ``EXPECTATIONS_CANCELLED_BY_RESET`` in handshake mode).
* Cycle mode (8.2) and handshake mode (8.3), first failure in trace order, and
  golden declaration order as the same-observation tie-break (section 10).
* Nothing compared is ``inconclusive`` (``NOTHING_COMPARED``), never a pass.

Limitation: the observations carry rising edges only, so clock ``x``
transitions are not visible here; Track A's provider covers that rule.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from src.contracts import (
    AnalysisStatus,
    ComparisonMode,
    ComparisonResult,
    Diagnostic,
    FailureAnchor,
    FailureKind,
    GoldenHeader,
    GoldenRecord,
    GoldenSignal,
    GoldenTrace,
    LogicValue,
    Observation,
    SamplingPhase,
)


class ObservedFormatError(ValueError):
    """The observed-samples file is missing, malformed, or out of order."""


@dataclass(frozen=True)
class ObservedEdge:
    time_ps: int
    pre: Mapping[str, str]
    settled: Mapping[str, str]


@dataclass(frozen=True)
class MappedEdge:
    epoch: int
    cycle: int
    edge: ObservedEdge


@dataclass
class EpochMap:
    edges: list[MappedEdge] = field(default_factory=list)
    # epoch -> time of the reset-active edge that ended it
    ended_at: dict[int, int] = field(default_factory=dict)
    # Set when no epoch could be mapped at all.
    diagnostic: Diagnostic | None = None
    # An x/z reset after reset was first asserted: mapping stops at that edge
    # and the checkers report it only if they reach it without a verdict.
    unknown_reset: Diagnostic | None = None


def _bits_field(obj: object, where: str) -> dict[str, str]:
    if not isinstance(obj, dict):
        raise ObservedFormatError(f"{where}: expected an object")
    out: dict[str, str] = {}
    for key, value in obj.items():
        if not isinstance(key, str) or not isinstance(value, str) or not value:
            raise ObservedFormatError(f"{where}: values must be non-empty bit strings")
        if set(value.lower()) - set("01xz"):
            raise ObservedFormatError(f"{where}: {key}={value!r} is not a 4-state bit string")
        out[key] = value.lower()
    return out


def parse_observed(path: Path) -> list[ObservedEdge]:
    """Parse the testbench's observed JSONL. Malformed input raises."""
    try:
        text = path.read_text()
    except OSError as exc:
        raise ObservedFormatError(f"{path}: cannot read ({exc})") from exc
    edges: list[ObservedEdge] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ObservedFormatError(f"{path}:{lineno}: invalid JSON") from exc
        if not isinstance(obj, dict):
            raise ObservedFormatError(f"{path}:{lineno}: expected an object")
        if obj.get("type") != "edge":
            continue
        t = obj.get("time_ps")
        if not isinstance(t, int) or isinstance(t, bool) or t < 0:
            raise ObservedFormatError(f"{path}:{lineno}: time_ps must be a non-negative integer")
        edges.append(
            ObservedEdge(
                time_ps=t,
                pre=_bits_field(obj.get("pre"), f"{path}:{lineno}.pre"),
                settled=_bits_field(obj.get("settled"), f"{path}:{lineno}.settled"),
            )
        )
    for a, b in zip(edges, edges[1:]):
        if b.time_ps <= a.time_ps:
            raise ObservedFormatError(f"{path}: edge times must strictly increase")
    if not edges:
        raise ObservedFormatError(f"{path}: no edges recorded")
    return edges


def map_epochs(edges: Sequence[ObservedEdge], reset_name: str, reset_active: str) -> EpochMap:
    result = EpochMap()
    seen_active = False
    in_epoch = False
    epoch = -1
    cycle = 0
    for e in edges:
        r = e.pre.get(reset_name)
        if r is None or r not in ("0", "1"):
            if not seen_active and r is not None:
                continue  # not yet in reset: nothing to compare, nothing lost
            result.unknown_reset = Diagnostic(
                code="RESET_UNKNOWN",
                message=f"reset {reset_name} is {r!r} before the edge at {e.time_ps} ps",
            )
            break
        if r == reset_active:
            if in_epoch:
                result.ended_at[epoch] = e.time_ps
            seen_active = True
            in_epoch = False
            continue
        if not seen_active:
            continue
        if not in_epoch:
            epoch += 1
            cycle = 0
            in_epoch = True
        else:
            cycle += 1
        result.edges.append(MappedEdge(epoch=epoch, cycle=cycle, edge=e))
    if epoch < 0:
        result.diagnostic = result.unknown_reset or Diagnostic(
            code="NO_EPOCH", message="reset release never observed"
        )
    return result


def _leaf(path: str) -> str:
    return path.rsplit(".", 1)[-1]


def _value(bits: str) -> LogicValue:
    return LogicValue(width=len(bits), bits=bits)


def _kind(actual: str) -> FailureKind:
    return FailureKind.UNKNOWN_VALUE if set(actual) & {"x", "z"} else FailureKind.VALUE_MISMATCH


def _non_verdict(
    status: AnalysisStatus, mode: ComparisonMode, code: str, message: str
) -> ComparisonResult:
    return ComparisonResult(
        schema_version=1,
        status=status,
        mode=mode,
        failure=None,
        compared_values=0,
        diagnostics=(Diagnostic(code=code, message=message),),
    )


def _pass_or_nothing(mode: ComparisonMode, compared: int) -> ComparisonResult:
    if compared == 0:
        return _non_verdict(
            AnalysisStatus.INCONCLUSIVE, mode, "NOTHING_COMPARED", "no expected value was compared"
        )
    return ComparisonResult(
        schema_version=1, status=AnalysisStatus.PASS, mode=mode, failure=None,
        compared_values=compared,
    )


def check_cycle(golden: GoldenTrace, edges: Sequence[ObservedEdge]) -> ComparisonResult:
    header = golden.header
    if header.mode is not ComparisonMode.CYCLE:
        raise ValueError("check_cycle requires a cycle-mode golden trace")
    mode = ComparisonMode.CYCLE
    emap = map_epochs(edges, _leaf(header.reset), header.reset_active)
    if emap.diagnostic is not None:
        return _non_verdict(AnalysisStatus.INCONCLUSIVE, mode, emap.diagnostic.code, emap.diagnostic.message)
    by_key = {(m.epoch, m.cycle): m for m in emap.edges}
    phase = header.sampling
    compared = 0
    for rec in golden.records:  # ordered by (epoch, cycle) = trace order
        m = by_key.get((rec.epoch, rec.index))
        if m is None:
            if rec.epoch in emap.ended_at:
                return _non_verdict(
                    AnalysisStatus.INCONCLUSIVE, mode, "EPOCH_ENDED_EARLY",
                    f"reset at {emap.ended_at[rec.epoch]} ps ended epoch {rec.epoch} "
                    f"before cycle {rec.index} was observed",
                )
            if emap.unknown_reset is not None:
                u = emap.unknown_reset
                return _non_verdict(AnalysisStatus.INCONCLUSIVE, mode, u.code, u.message)
            return _non_verdict(
                AnalysisStatus.INCONCLUSIVE, mode, "COVERAGE",
                f"epoch {rec.epoch} cycle {rec.index} is not in the observed samples",
            )
        sample = m.edge.settled if phase is SamplingPhase.SETTLED else m.edge.pre
        failures: list[FailureAnchor] = []
        for sig in header.signals:
            expected = rec.values.get(sig.name)
            if expected is None:
                continue
            actual = sample.get(sig.name)
            if actual is None:
                return _non_verdict(
                    AnalysisStatus.INCONCLUSIVE, mode, "SIGNAL_NOT_SAMPLED",
                    f"{sig.name} missing from the {phase.value} sample at {m.edge.time_ps} ps",
                )
            if len(actual) != sig.width:
                return _non_verdict(
                    AnalysisStatus.INVALID_INPUT, mode, "WIDTH_MISMATCH",
                    f"{sig.name} sampled with width {len(actual)}, declared {sig.width}",
                )
            compared += 1
            if actual != expected:
                failures.append(
                    FailureAnchor(
                        kind=_kind(actual), mode=mode, signal_path=sig.path,
                        clock_path=header.clock,
                        observation=Observation(
                            epoch=m.epoch, cycle=m.cycle, phase=phase, time_ps=m.edge.time_ps
                        ),
                        expected=_value(expected), actual=_value(actual),
                        detail=f"{sig.name} expected {expected} actual {actual}",
                    )
                )
        if failures:
            return ComparisonResult(
                schema_version=1, status=AnalysisStatus.FAIL, mode=mode,
                failure=failures[0], additional_mismatches=tuple(failures[1:]),
                compared_values=compared,
            )
    return _pass_or_nothing(mode, compared)


def check_handshake(golden: GoldenTrace, edges: Sequence[ObservedEdge]) -> ComparisonResult:
    header = golden.header
    if header.mode is not ComparisonMode.HANDSHAKE or header.valid is None or header.ready is None:
        raise ValueError("check_handshake requires a handshake-mode golden trace")
    mode = ComparisonMode.HANDSHAKE
    payload = header.signals[0]
    valid_name, ready_name = _leaf(header.valid), _leaf(header.ready)
    emap = map_epochs(edges, _leaf(header.reset), header.reset_active)
    if emap.diagnostic is not None:
        return _non_verdict(AnalysisStatus.INCONCLUSIVE, mode, emap.diagnostic.code, emap.diagnostic.message)
    pending: dict[int, list[GoldenRecord]] = {}
    for rec in golden.records:
        pending.setdefault(rec.epoch, []).append(rec)
    final_epoch = max(pending)
    compared = 0
    deadline_seen = False
    current_epoch: int | None = None

    def cancelled(epoch: int) -> ComparisonResult | None:
        left = pending.get(epoch)
        if left and epoch in emap.ended_at:
            return _non_verdict(
                AnalysisStatus.INCONCLUSIVE, mode, "EXPECTATIONS_CANCELLED_BY_RESET",
                f"reset at {emap.ended_at[epoch]} ps ended epoch {epoch} with "
                f"{len(left)} transaction(s) outstanding",
            )
        return None

    for m in emap.edges:
        if current_epoch is not None and m.epoch != current_epoch:
            early = cancelled(current_epoch)
            if early is not None:
                return early
        current_epoch = m.epoch
        v, r = m.edge.pre.get(valid_name), m.edge.pre.get(ready_name)
        if v is None or r is None:
            return _non_verdict(
                AnalysisStatus.INCONCLUSIVE, mode, "SIGNAL_NOT_SAMPLED",
                f"valid/ready not sampled before the edge at {m.edge.time_ps} ps",
            )
        if v == "0" or r == "0":
            transfer = False
        elif v == "1" and r == "1":
            transfer = True
        else:
            return _non_verdict(
                AnalysisStatus.INCONCLUSIVE, mode, "HANDSHAKE_UNKNOWN",
                f"valid={v} ready={r} at {m.edge.time_ps} ps; transfers are never guessed",
            )
        obs = Observation(epoch=m.epoch, cycle=m.cycle, phase=SamplingPhase.BEFORE, time_ps=m.edge.time_ps)
        epoch_pending = pending.get(m.epoch, [])
        if transfer:
            actual = m.edge.pre.get(payload.name)
            if actual is None:
                return _non_verdict(
                    AnalysisStatus.INCONCLUSIVE, mode, "SIGNAL_NOT_SAMPLED",
                    f"{payload.name} not sampled before the edge at {m.edge.time_ps} ps",
                )
            total = sum(1 for rec in golden.records if rec.epoch == m.epoch)
            if not epoch_pending:
                return _hs_fail(
                    FailureKind.EXTRA_TRANSACTION, payload, header, obs, total - len(epoch_pending),
                    None, _value(actual), f"unexpected transfer {actual}", compared,
                )
            rec = epoch_pending.pop(0)
            expected = rec.values[payload.name]
            compared += 1
            if actual != expected:
                return _hs_fail(
                    _kind(actual), payload, header, obs, rec.index,
                    _value(expected), _value(actual),
                    f"transaction {rec.index} expected {expected} actual {actual}", compared,
                )
        if header.deadline_cycle is not None and m.epoch == final_epoch and m.cycle == header.deadline_cycle:
            deadline_seen = True
            if epoch_pending:
                rec = epoch_pending[0]
                return _hs_fail(
                    FailureKind.MISSING_TRANSACTION, payload, header, obs, rec.index,
                    _value(rec.values[payload.name]), None,
                    f"transaction {rec.index} missing at deadline cycle {m.cycle}", compared,
                )
    if current_epoch is not None:
        early = cancelled(current_epoch)
        if early is not None:
            return early
    unfinished = any(pending.values()) or (header.deadline_cycle is not None and not deadline_seen)
    if unfinished and emap.unknown_reset is not None:
        u = emap.unknown_reset
        return _non_verdict(AnalysisStatus.INCONCLUSIVE, mode, u.code, u.message)
    if unfinished:
        return _non_verdict(
            AnalysisStatus.INCONCLUSIVE, mode, "COVERAGE",
            "the observed samples end before the deadline with expectations outstanding"
            if header.deadline_cycle is not None
            else "no deadline declared and expectations remain",
        )
    return _pass_or_nothing(mode, compared)


def _hs_fail(
    kind: FailureKind,
    payload: GoldenSignal,
    header: GoldenHeader,
    obs: Observation,
    txn: int,
    expected: LogicValue | None,
    actual: LogicValue | None,
    detail: str,
    compared: int,
) -> ComparisonResult:
    return ComparisonResult(
        schema_version=1, status=AnalysisStatus.FAIL, mode=ComparisonMode.HANDSHAKE,
        failure=FailureAnchor(
            kind=kind, mode=ComparisonMode.HANDSHAKE, signal_path=payload.path,
            clock_path=header.clock, observation=obs, transaction_index=txn,
            expected=expected, actual=actual, detail=detail,
        ),
        compared_values=compared,
    )
