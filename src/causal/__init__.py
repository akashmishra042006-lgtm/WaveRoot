"""Track B: dependency graph, value-pruned backward slicing, assertion synthesis.

Typical use, from the RTL files and a failure anchor::

    report = analyze_failure(
        failure,
        provider,
        ["rtl/mutant/fifo.sv"],
        root=case_dir,
        options=SliceOptions(trace_scope="tb.dut"),
    )

or in two steps, to reuse one design graph for several failures::

    design = build_design_graph([rtl_file], root=repo_root)
    report = slice_failure(failure, provider, design, SliceOptions(trace_scope="tb.dut"))

Only ``src.contracts`` types cross the package boundary.
"""

from src.causal.ast_graph import DesignError, DesignGraph, build_design_graph
from src.causal.backward_slicer import (
    SliceOptions,
    analyze_comparison,
    analyze_failure,
    slice_failure,
)
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
    "analyze_failure",
    "build_design_graph",
    "dump_design_graph",
    "load_design_graph",
    "slice_failure",
    "synthesize_assertion",
    "verify_sources",
]
