"""Generate the UI's test fixtures through the canonical Python contracts.

Run from the repository root:

    uv run python ui/scripts/make_fixtures.py [--skip-real]

* Valid fixtures are built by transforming the shared mock fixture and are
  re-validated with ``src.contracts`` before they are written, so the UI is
  tested against exactly what the backend accepts.
* Invalid fixtures are written deliberately broken, and the script asserts that
  ``src.contracts`` rejects each one too.
* ``real_*.json`` are genuine run records from ``python -m src.app.cli analyze``
  (real Verilator/Icarus runs). Only machine-local paths are rewritten to be
  repository-relative; statuses, values, and hashes are untouched.
"""

from __future__ import annotations

import argparse
import copy
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from src.contracts import CausalReport, load_mock_fixture  # noqa: E402

OUT = REPO / "ui" / "fixtures"
MOCK = REPO / "tests" / "fixtures" / "mock_fifo_failure.json"


def mock_report() -> dict[str, Any]:
    fixture = load_mock_fixture(MOCK)
    data: dict[str, Any] = json.loads(fixture.report.model_dump_json())
    return data


def write_valid(name: str, report: dict[str, Any]) -> None:
    CausalReport.model_validate_json(json.dumps(report))  # must be accepted
    (OUT / name).write_text(json.dumps(report, indent=2) + "\n")


def write_invalid(name: str, report: dict[str, Any]) -> None:
    try:
        CausalReport.model_validate_json(json.dumps(report))
    except ValueError:
        (OUT / name).write_text(json.dumps(report, indent=2) + "\n")
        return
    raise SystemExit(f"{name}: the Python contracts unexpectedly accept this fixture")


def shift_times(obj: Any, offset: int) -> Any:
    """Add ``offset`` to every timestamp field (decimal strings)."""
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            if key in ("time_ps", "start_ps", "end_ps") and isinstance(value, str):
                out[key] = str(int(value) + offset)
            else:
                out[key] = shift_times(value, offset)
        return out
    if isinstance(obj, list):
        return [shift_times(v, offset) for v in obj]
    return obj


def synthetic(report: dict[str, Any], note: str) -> dict[str, Any]:
    report["provenance"] = {"kind": "synthetic_fixture", "case_id": "ui_fixture", "note": note}
    return report


def make_synthetic() -> None:
    base = mock_report()

    # Inconclusive: no failure and no graph, limits explained.
    write_valid("inconclusive.json", synthetic({
        "schema_version": 1, "provenance": {}, "status": "inconclusive", "failure": None,
        "anchor_node_id": None, "nodes": [], "edges": [], "suspects": [], "sources": [],
        "assertions": [], "waveform": None, "truncated": False,
        "diagnostics": [{"code": "COVERAGE", "message": "The trace ends before cycle 5 of epoch 0 was observed."}],
    }, "UI fixture: inconclusive comparison with no graph."))

    # Source unavailable: the top suspect has no location, the graph is truncated.
    unavailable = copy.deepcopy(base)
    unavailable["suspects"][0]["location"] = None
    unavailable["diagnostics"].append({"code": "SOURCE_UNAVAILABLE", "message": "No source span could be mapped for suspect s1."})
    write_valid("source_unavailable.json", synthetic(unavailable, "UI fixture: mock graph with an unmapped suspect location."))

    # Large time: every timestamp shifted beyond 2**53.
    large = shift_times(copy.deepcopy(base), 2**60)
    write_valid("large_time.json", synthetic(large, "UI fixture: mock graph with timestamps above 2**53."))

    # A candidate assertion that has not been validated, and one that has.
    asserted = copy.deepcopy(base)
    asserted["assertions"] = [
        {"id": "a1", "text": "assert property (@(posedge clk) disable iff (!rst_n) (wptr[1:0] == 2'b11 && push) |=> wptr[2] != $past(wptr[2]));",
         "validation": "candidate", "tool": None},
        {"id": "a2", "text": "assert property (@(posedge clk) full |-> !in_ready || pop);",
         "validation": "validated", "tool": "example: Icarus 12.0 positive and negative runs (fixture)"},
    ]
    write_valid("assertions.json", synthetic(asserted, "UI fixture: mock graph with candidate assertions."))

    # Invalid fixtures: each must be rejected by the Python contracts as well.
    bad_width = copy.deepcopy(base)
    bad_width["nodes"][1]["value"] = {"width": 3, "bits": "00"}
    write_invalid("invalid_width.json", bad_width)

    dangling = copy.deepcopy(base)
    dangling["edges"][0]["target"] = "missing.node"
    write_invalid("dangling_node.json", dangling)

    future = copy.deepcopy(base)
    future["schema_version"] = 2
    write_invalid("unknown_schema.json", future)


def make_real() -> None:
    runs = [
        ("real_fifo_pass.json", "case_01_fifo_wrap", "correct", []),
        ("real_fifo_fail.json", "case_01_fifo_wrap", "mutant", []),
        ("real_fifo_fail_fixture_causal.json", "case_01_fifo_wrap", "mutant",
         ["--fixture-causal", str(MOCK)]),
        ("real_alu_x.json", "case_04_alu_xprop", "mutant", []),
        # A genuine infrastructure error: the wall-clock limit expires mid-build.
        ("real_fifo_timeout.json", "case_01_fifo_wrap", "mutant", ["--timeout", "0.05"]),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        for name, case, variant, extra in runs:
            proc = subprocess.run(
                [sys.executable, "-m", "src.app.cli", "analyze",
                 f"benchmarks/{case}/manifest.json", "--variant", variant,
                 "--schedule", "directed", "--artifacts", tmp, "--json", *extra],
                cwd=REPO, capture_output=True, text=True, timeout=600,
            )
            if proc.returncode not in (0, 1, 2):
                raise SystemExit(f"{name}: CLI exited {proc.returncode}\n{proc.stderr}")
            text = proc.stdout.replace(str(REPO) + "/", "").replace(tmp + "/", "artifacts/")
            record = json.loads(text)
            if record["run"] and record["run"]["run_dir"]:
                record["run"]["run_dir"] = "artifacts/" + Path(record["run"]["run_dir"]).name
            (OUT / name).write_text(json.dumps(record, indent=2) + "\n")
            print(f"{name}: {record['status']}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-real", action="store_true")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    make_synthetic()
    if not args.skip_real:
        make_real()
    print(f"fixtures written to {OUT.relative_to(REPO)}")


if __name__ == "__main__":
    main()
