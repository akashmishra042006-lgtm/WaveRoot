"""Track A: waveform parsing, time queries, and golden-model comparison."""

from src.waveform.diff_engine import compare, compare_files
from src.waveform.parser import (
    InvalidTraceError,
    ParsedTrace,
    SkippedVariable,
    UnsupportedTraceError,
    VcdWaveformProvider,
    parse_vcd,
)

__all__ = [
    "InvalidTraceError",
    "ParsedTrace",
    "SkippedVariable",
    "UnsupportedTraceError",
    "VcdWaveformProvider",
    "compare",
    "compare_files",
    "parse_vcd",
]
