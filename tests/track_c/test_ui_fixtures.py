"""The UI's committed fixtures stay consistent with the Python contracts.

Valid fixtures must validate; deliberately invalid ones must be rejected by
``src.contracts`` exactly as the UI rejects them. Run records re-validate
through the service's ``load_report``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.app.service import load_report
from src.contracts import CausalReport

FIXTURES = Path(__file__).resolve().parents[2] / "ui" / "fixtures"

VALID_REPORTS = ["inconclusive.json", "source_unavailable.json", "large_time.json", "assertions.json"]
INVALID_REPORTS = ["invalid_width.json", "dangling_node.json", "unknown_schema.json"]
RUN_RECORDS = sorted(p.name for p in FIXTURES.glob("real_*.json"))


@pytest.mark.parametrize("name", VALID_REPORTS)
def test_valid_report_fixtures_validate(name: str) -> None:
    report = CausalReport.model_validate_json((FIXTURES / name).read_text())
    assert report.provenance.kind.value == "synthetic_fixture"


@pytest.mark.parametrize("name", INVALID_REPORTS)
def test_invalid_report_fixtures_are_rejected(name: str) -> None:
    with pytest.raises(ValueError):
        CausalReport.model_validate_json((FIXTURES / name).read_text())


def test_real_run_records_exist() -> None:
    assert {"real_fifo_pass.json", "real_fifo_fail.json", "real_alu_x.json"} <= set(RUN_RECORDS)


@pytest.mark.parametrize("name", RUN_RECORDS)
def test_real_run_records_validate(name: str) -> None:
    data = load_report(FIXTURES / name)
    assert data["comparison_adapter"] == {"name": "track_c_selfcheck", "kind": "benchmark_self_check"}
    assert "/home/" not in (FIXTURES / name).read_text()


def test_large_time_fixture_exceeds_float_precision() -> None:
    data = json.loads((FIXTURES / "large_time.json").read_text())
    assert int(data["failure"]["observation"]["time_ps"]) > 2**53
