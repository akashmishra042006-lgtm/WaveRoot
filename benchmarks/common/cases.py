"""Registry of the four Track C benchmark cases.

Used by the tests and by future Track C tooling. Each entry names the RTL for
both variants, the required simulator, the schedules, and — for schedules where
the answer is documented — whether the mutant must be detected and at which
anchor. ``mutant_detected=False`` schedules are intentional masking controls:
a mutant pass there is the expected result.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from benchmarks.case_01_fifo_wrap import golden_model as fifo_golden
from benchmarks.case_02_axi_skid import golden_model as elastic_golden
from benchmarks.case_03_mac_overflow import golden_model as mac_golden
from benchmarks.case_04_alu_xprop import golden_model as alu_golden

BENCH_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class ExpectedAnchor:
    mode: str  # "cycle" or "handshake"
    kind: str
    signal_path: str
    epoch: int
    cycle: int
    time_ps: int
    expected_bits: str | None
    actual_bits: str | None


@dataclass(frozen=True)
class Schedule:
    name: str
    mutant_detected: bool
    # Verified anchor for the mutant's cycle-mode first failure, when detected.
    anchor: ExpectedAnchor | None = None


@dataclass(frozen=True)
class Case:
    case_id: str
    rtl_name: str
    simulator: str  # "verilator" or "icarus"
    golden: ModuleType
    schedules: tuple[Schedule, ...]
    has_handshake: bool

    @property
    def directory(self) -> Path:
        return BENCH_ROOT / self.case_id

    def rtl(self, variant: str) -> Path:
        return self.directory / "rtl" / variant / f"{self.rtl_name}.sv"

    def tb(self) -> Path:
        return self.directory / "tb" / "tb.sv"

    def stim(self, schedule: str) -> Path:
        return self.directory / "stim" / f"{schedule}.stim"


CASES: tuple[Case, ...] = (
    Case(
        case_id="case_01_fifo_wrap",
        rtl_name="fifo",
        simulator="verilator",
        golden=fifo_golden,
        has_handshake=True,
        schedules=(
            Schedule(
                "directed",
                mutant_detected=True,
                anchor=ExpectedAnchor(
                    mode="cycle", kind="value_mismatch", signal_path="tb.dut.full",
                    epoch=0, cycle=3, time_ps=55_000, expected_bits="1", actual_bits="0",
                ),
            ),
            Schedule(
                "full_hold",
                mutant_detected=True,
                anchor=ExpectedAnchor(
                    mode="cycle", kind="value_mismatch", signal_path="tb.dut.full",
                    epoch=0, cycle=3, time_ps=55_000, expected_bits="1", actual_bits="0",
                ),
            ),
            Schedule("multi_wrap", mutant_detected=False),
            Schedule("reset_midway", mutant_detected=False),
        ),
    ),
    Case(
        case_id="case_02_axi_skid",
        rtl_name="elastic",
        simulator="verilator",
        golden=elastic_golden,
        has_handshake=True,
        schedules=(
            Schedule(
                "directed",
                mutant_detected=True,
                anchor=ExpectedAnchor(
                    mode="cycle", kind="value_mismatch", signal_path="tb.dut.m_valid",
                    epoch=0, cycle=1, time_ps=35_000, expected_bits="1", actual_bits="0",
                ),
            ),
            Schedule(
                "long_stall",
                mutant_detected=True,
                anchor=ExpectedAnchor(
                    mode="cycle", kind="value_mismatch", signal_path="tb.dut.m_valid",
                    epoch=0, cycle=1, time_ps=35_000, expected_bits="1", actual_bits="0",
                ),
            ),
            Schedule(
                "stall_release_replace",
                mutant_detected=True,
                anchor=ExpectedAnchor(
                    mode="cycle", kind="value_mismatch", signal_path="tb.dut.m_valid",
                    epoch=0, cycle=1, time_ps=35_000, expected_bits="1", actual_bits="0",
                ),
            ),
            Schedule(
                "reset_midway",
                mutant_detected=True,
                anchor=ExpectedAnchor(
                    mode="cycle", kind="value_mismatch", signal_path="tb.dut.m_valid",
                    epoch=0, cycle=2, time_ps=45_000, expected_bits="1", actual_bits="0",
                ),
            ),
        ),
    ),
    Case(
        case_id="case_03_mac_overflow",
        rtl_name="mac",
        simulator="verilator",
        golden=mac_golden,
        has_handshake=False,
        schedules=(
            Schedule(
                "directed",
                mutant_detected=True,
                anchor=ExpectedAnchor(
                    mode="cycle", kind="value_mismatch", signal_path="tb.dut.out_data",
                    epoch=0, cycle=3, time_ps=55_000,
                    expected_bits="1000000000000000", actual_bits="0100010010000000",
                ),
            ),
            Schedule(
                "pos_saturation",
                mutant_detected=True,
                anchor=ExpectedAnchor(
                    mode="cycle", kind="value_mismatch", signal_path="tb.dut.out_data",
                    epoch=0, cycle=3, time_ps=55_000,
                    expected_bits="0111111111111111", actual_bits="1011110100000011",
                ),
            ),
            Schedule(
                "neg_boundary",
                mutant_detected=True,
                anchor=ExpectedAnchor(
                    mode="cycle", kind="value_mismatch", signal_path="tb.dut.out_data",
                    epoch=0, cycle=5, time_ps=75_000,
                    expected_bits="1000000000000000", actual_bits="0110000000000000",
                ),
            ),
            Schedule("mixed_bubbles", mutant_detected=False),
            Schedule("reset_flush", mutant_detected=False),
        ),
    ),
    Case(
        case_id="case_04_alu_xprop",
        rtl_name="alu",
        simulator="icarus",
        golden=alu_golden,
        has_handshake=False,
        schedules=(
            Schedule(
                "directed",
                mutant_detected=True,
                anchor=ExpectedAnchor(
                    mode="cycle", kind="unknown_value", signal_path="tb.dut.out_data",
                    epoch=0, cycle=0, time_ps=25_000,
                    expected_bits="00000000", actual_bits="xxxxxxxx",
                ),
            ),
            Schedule(
                "partial_overlap",
                mutant_detected=True,
                anchor=ExpectedAnchor(
                    mode="cycle", kind="unknown_value", signal_path="tb.dut.out_data",
                    epoch=0, cycle=0, time_ps=25_000,
                    expected_bits="01010000", actual_bits="0101xxxx",
                ),
            ),
            Schedule(
                "reset_midway",
                mutant_detected=True,
                anchor=ExpectedAnchor(
                    mode="cycle", kind="value_mismatch", signal_path="tb.dut.out_data",
                    epoch=1, cycle=0, time_ps=65_000,
                    expected_bits="11001100", actual_bits="11011101",
                ),
            ),
        ),
    ),
)


def case_by_id(case_id: str) -> Case:
    for case in CASES:
        if case.case_id == case_id:
            return case
    raise KeyError(case_id)
