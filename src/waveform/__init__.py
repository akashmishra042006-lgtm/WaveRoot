"""Track A: waveform parsing, time queries, and golden-model comparison."""

from src.waveform.diff_engine import compare, compare_files
from src.waveform.fst import (
    ConversionFailedError,
    ConverterUnavailableError,
    convert_fst_to_vcd_text,
    load_trace,
)
from src.waveform.parser import (
    InvalidTraceError,
    ParsedTrace,
    SkippedVariable,
    UnsupportedTraceError,
    VcdWaveformProvider,
    parse_vcd,
)
from src.waveform.select import match_paths

__all__ = [
    "ConversionFailedError",
    "ConverterUnavailableError",
    "InvalidTraceError",
    "ParsedTrace",
    "SkippedVariable",
    "UnsupportedTraceError",
    "VcdWaveformProvider",
    "compare",
    "compare_files",
    "convert_fst_to_vcd_text",
    "load_trace",
    "match_paths",
    "parse_vcd",
]
