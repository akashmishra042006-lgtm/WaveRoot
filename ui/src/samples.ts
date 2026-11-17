// Documents bundled with the viewer. Every entry is labeled with what it is;
// opening one never launches a simulation.

import mockFifo from "../../tests/fixtures/mock_fifo_failure.json";
import mockFifoSource from "../../tests/fixtures/mock_fifo_wrap.sv?raw";
import realFifoFail from "../fixtures/real_fifo_fail.json";
import realFifoPass from "../fixtures/real_fifo_pass.json";
import realAluX from "../fixtures/real_alu_x.json";
import realTimeout from "../fixtures/real_fifo_timeout.json";
import inconclusive from "../fixtures/inconclusive.json";
import sourceUnavailable from "../fixtures/source_unavailable.json";
import largeTime from "../fixtures/large_time.json";
import assertions from "../fixtures/assertions.json";

export interface Sample {
  id: string;
  label: string;
  group: "sample" | "real" | "state";
  document: unknown;
}

export const SAMPLES: readonly Sample[] = [
  { id: "mock-fifo", label: "Mock FIFO failure (synthetic)", group: "sample", document: mockFifo },
  { id: "real-fifo-fail", label: "FIFO mutant, real Verilator run", group: "real", document: realFifoFail },
  { id: "real-fifo-pass", label: "FIFO correct, real Verilator run", group: "real", document: realFifoPass },
  { id: "real-alu-x", label: "ALU unknown value, real Icarus run", group: "real", document: realAluX },
  { id: "real-timeout", label: "FIFO run that timed out (real infrastructure error)", group: "real", document: realTimeout },
  { id: "state-inconclusive", label: "Inconclusive (synthetic)", group: "state", document: inconclusive },
  { id: "state-source", label: "Source unavailable (synthetic)", group: "state", document: sourceUnavailable },
  { id: "state-large", label: "Timestamps above 2^53 (synthetic)", group: "state", document: largeTime },
  { id: "state-assertions", label: "Candidate assertions (synthetic)", group: "state", document: assertions },
];

export const GROUP_LABELS: Record<Sample["group"], string> = {
  sample: "Sample data",
  real: "Bundled real run records",
  state: "Interface state samples",
};

/** Source text shipped with the viewer for the bundled samples. Its hash is
 * computed at load time and must match the report; it is not trusted by name. */
export const BUNDLED_SOURCES: readonly { name: string; text: string }[] = [
  { name: "tests/fixtures/mock_fifo_wrap.sv", text: mockFifoSource },
];
