"""Track B: dependency graph, value-pruned backward slicing, assertion synthesis.

Typical use::

    design = build_design_graph([rtl_file], root=repo_root)
    report = slice_failure(failure, provider, design, SliceOptions(trace_scope="tb.dut"))

Only ``src.contracts`` types cross the package boundary.
"""

from src.causal.ast_graph import DesignError, DesignGraph, build_design_graph
from src.causal.backward_slicer import SliceOptions, analyze_comparison, slice_failure
from src.causal.graph_io import (
    GraphFormatError,
    dump_design_graph,
    load_design_graph,
    verify_sources,
)
from src.causal.sva_synth import SvaResult, synthesize_assertion

__all__ = [
    "DesignError",
    "DesignGraph",
    "GraphFormatError",
    "SliceOptions",
    "SvaResult",
    "analyze_comparison",
    "build_design_graph",
    "dump_design_graph",
    "load_design_graph",
    "slice_failure",
    "synthesize_assertion",
    "verify_sources",
]
