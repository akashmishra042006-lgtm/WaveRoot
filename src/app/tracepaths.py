"""Mapping between canonical benchmark paths and a trace's hierarchy.

spec.md section 3.1: Track C supplies the mapping between RTL hierarchy and
trace hierarchy. Golden models emit canonical ``tb.dut.*`` paths; each run
detects the scope prefix its simulator added (Verilator: ``TOP.``; Icarus:
none) and goldens are rebased onto it, so every failure anchor resolves in
the trace it came from (section 5.1). The prefix is detected, never assumed.

This reads only the VCD header's scope declarations. It is not a waveform
parser; values come from Track A's provider.
"""

from __future__ import annotations

from pathlib import Path

from src.contracts import GoldenTrace, parse_golden_jsonl

CANONICAL_CLOCK = "tb.dut.clk"


class TraceHierarchyError(ValueError):
    """The trace does not declare the canonical hierarchy."""


def vcd_declared_paths(vcd: Path) -> set[str]:
    """Signal paths declared in the VCD header, as dotted hierarchy names."""
    paths: set[str] = set()
    stack: list[str] = []
    with vcd.open() as fh:
        for line in fh:
            tok = line.split()
            if not tok:
                continue
            if tok[0] == "$scope" and len(tok) >= 3:
                stack.append(tok[2])
            elif tok[0] == "$upscope":
                if stack:
                    stack.pop()
            elif tok[0] == "$var" and len(tok) >= 5:
                paths.add(".".join([*stack, tok[4]]))
            elif tok[0] == "$enddefinitions":
                break
    return paths


def detect_trace_prefix(vcd: Path, canonical: str = CANONICAL_CLOCK) -> str:
    """The scope prefix prepended to ``canonical`` in this trace ("" if none).

    Raises ``TraceHierarchyError`` unless exactly one declared path ends with
    the canonical path, so an ambiguous hierarchy is never guessed.
    """
    declared = vcd_declared_paths(vcd)
    matches = sorted(p for p in declared if p == canonical or p.endswith("." + canonical))
    if len(matches) != 1:
        raise TraceHierarchyError(
            f"{vcd.name}: expected exactly one path ending in {canonical!r}, found {len(matches)}"
        )
    path = matches[0]
    return "" if path == canonical else path[: -len(canonical)]


def rebase_golden(trace: GoldenTrace, prefix: str) -> GoldenTrace:
    """Prefix every path in a golden header; re-validated by the contract parser."""
    if not prefix:
        return trace
    if not prefix.endswith(".") or prefix.startswith("."):
        raise ValueError(f"invalid scope prefix {prefix!r}")
    h = trace.header
    header = h.model_copy(
        update={
            "clock": prefix + h.clock,
            "reset": prefix + h.reset,
            "signals": tuple(s.model_copy(update={"path": prefix + s.path}) for s in h.signals),
            "valid": prefix + h.valid if h.valid else None,
            "ready": prefix + h.ready if h.ready else None,
        }
    )
    return parse_golden_jsonl(GoldenTrace(header=header, records=trace.records).to_jsonl())
