"""Deterministic held-out stimulus for the four benchmark cases (C07).

Every schedule is a pure function of ``(case, index)``: a fixed seed, the
profile table below, and nothing else. The generator never runs a simulator
and never looks at a mutant: its only feedback is the *reference* model, used
to reject degenerate schedules (a handshake epoch with no transfer, which
spec 8.1 forbids). Whether the mutant is detected is therefore a measured
outcome, not something the schedules were chosen to produce.

Author note: these were written by the same session that built the tools they
exercise, so they are held out from *tuning*, not from *authorship*. An
independent reviewer should read the profile table and add or replace profiles.

    uv run python -m benchmarks.held_out.generate           # (re)write files + manifests
    uv run python -m benchmarks.held_out.generate --check   # fail if anything differs
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from benchmarks.common import benchlib
from benchmarks.common.cases import BENCH_ROOT, CASES, Case

LENGTH = 40  # random cycles, then DRAIN no-input cycles; under the 64-line limit
DRAIN = 8
MAX_ATTEMPTS = 50  # deterministic re-seeds if the reference rejects a schedule


@dataclass(frozen=True)
class Profile:
    """Knobs for one schedule. ``reset_at`` is the cycle whose row asserts reset
    (0 = none); the reset lasts ``reset_len`` cycles."""

    stay_valid: float  # Markov stickiness of the input-valid line
    p_valid: float
    stay_ready: float  # stickiness of the sink-ready line (stall run lengths)
    p_ready: float
    data: str = "random"
    reset_at: int = 0
    reset_len: int = 2
    config_at: int = 0  # ALU only: cycle of the (single) selector write


PROFILES: dict[str, tuple[Profile, ...]] = {
    "case_01_fifo_wrap": (
        Profile(0.8, 0.9, 0.6, 0.35),
        Profile(0.5, 0.6, 0.5, 0.6),
        Profile(0.6, 0.35, 0.8, 0.9),
        Profile(0.9, 1.0, 0.5, 0.5),
        Profile(0.7, 0.8, 0.7, 0.2, reset_at=17),
    ),
    "case_02_axi_skid": (
        Profile(0.8, 0.9, 0.6, 0.4),
        Profile(0.5, 0.6, 0.5, 0.6),
        Profile(0.6, 0.5, 0.85, 0.8),
        Profile(0.9, 1.0, 0.8, 0.3),
        Profile(0.7, 0.8, 0.5, 0.5, reset_at=15),
    ),
    "case_03_mac_overflow": (
        Profile(0.7, 0.8, 0, 0, data="small"),
        Profile(0.7, 0.8, 0, 0, data="full"),
        Profile(0.8, 0.9, 0, 0, data="extreme"),
        Profile(0.5, 0.6, 0, 0, data="mixed"),
        Profile(0.7, 0.8, 0, 0, data="extreme", reset_at=14),
    ),
    "case_04_alu_xprop": (
        Profile(0.6, 0.7, 0, 0, data="random", config_at=10),
        Profile(0.6, 0.7, 0, 0, data="random", config_at=1),
        Profile(0.6, 0.7, 0, 0, data="equal", config_at=8),
        Profile(0.6, 0.7, 0, 0, data="nibble", config_at=6),
        Profile(0.6, 0.7, 0, 0, data="random", config_at=5, reset_at=20),
    ),
}


def _markov(rng: random.Random, stay: float, p_one: float, n: int) -> list[int]:
    """A 0/1 line with run structure: keep the previous value with probability
    ``stay``, otherwise redraw it at density ``p_one``."""
    out: list[int] = []
    cur = 1 if rng.random() < p_one else 0
    for _ in range(n):
        if rng.random() >= stay:
            cur = 1 if rng.random() < p_one else 0
        out.append(cur)
    return out


def _int8(rng: random.Random, kind: str) -> int:
    if kind == "small":
        return rng.randint(-15, 15) & 0xFF
    if kind == "extreme":
        return rng.choice((127, -128, 126, -127, 64, -64)) & 0xFF
    if kind == "mixed":
        return rng.choice((rng.randint(-15, 15), rng.randint(-128, 127), 127, -128)) & 0xFF
    return rng.randint(-128, 127) & 0xFF


def _rows(case_id: str, prof: Profile, rng: random.Random) -> list[list[str]]:
    n = LENGTH
    valid = _markov(rng, prof.stay_valid, prof.p_valid, n)
    ready = _markov(rng, prof.stay_ready, prof.p_ready, n)
    rst = [1] * n
    if prof.reset_at:
        for i in range(prof.reset_at, min(n, prof.reset_at + prof.reset_len)):
            rst[i] = 0
    rows: list[list[str]] = []
    for i in range(n):
        r = str(rst[i])
        if case_id == "case_01_fifo_wrap":
            rows.append([r, str(valid[i]), f"{rng.randint(1, 255):02X}", str(ready[i])])
        elif case_id == "case_02_axi_skid":
            rows.append([r, str(valid[i]), f"{rng.randint(1, 255):02X}", str(ready[i])])
        elif case_id == "case_03_mac_overflow":
            rows.append([r, str(valid[i]), f"{_int8(rng, prof.data):02X}", f"{_int8(rng, prof.data):02X}"])
        else:
            a = rng.randint(0, 255)
            if prof.data == "equal" and rng.random() < 0.5:
                b = a
            elif prof.data == "nibble":
                b = (a & 0xF0) | rng.randint(0, 15)
            else:
                b = rng.randint(0, 255)
            we = 1 if i == prof.config_at else 0
            rows.append([r, str(valid[i]), f"{a:02X}", f"{b:02X}", str(we), str(rng.randint(0, 1) if we else 0)])
    # Drain: no input, sink ready, so buffered work completes inside the window.
    tail = {
        "case_01_fifo_wrap": ["1", "0", "00", "1"],
        "case_02_axi_skid": ["1", "0", "00", "1"],
        "case_03_mac_overflow": ["1", "0", "00", "00"],
        "case_04_alu_xprop": ["1", "0", "00", "00", "0", "0"],
    }[case_id]
    rows.extend([list(tail) for _ in range(DRAIN)])
    return rows


def _render(case: Case, idx: int, seed: int, attempt: int, prof: Profile, rows: list[list[str]], columns: tuple[str, ...]) -> str:
    head = [
        f"# {case.case_id} held-out schedule {idx:02d} (benchmarks/held_out/generate.py)",
        f"# seed {seed}, attempt {attempt}; profile {prof}",
        "# Columns: rst_n " + " ".join(columns),
    ]
    return "\n".join([*head, *(" ".join(r) for r in rows)]) + "\n"


def _columns(case: Case) -> tuple[str, ...]:
    return tuple(case.golden.COLUMNS)


def generate_case(case: Case, idx: int) -> tuple[str, int]:
    """Text of held-out schedule ``idx`` (1-based) and the attempt that was kept."""
    prof = PROFILES[case.case_id][idx - 1]
    cols = _columns(case)
    base_seed = int.from_bytes(f"{case.case_id}/{idx}".encode(), "big") % (2**32)
    for attempt in range(MAX_ATTEMPTS):
        seed = base_seed + attempt
        rows = _rows(case.case_id, prof, random.Random(seed))
        text = _render(case, idx, seed, attempt, prof, rows, cols)
        tmp = Path(f"/tmp/_held_out_{case.case_id}_{idx}.stim")
        tmp.write_text(text)
        try:
            case.golden.goldens_for(tmp)  # reference model only; never a mutant
        except ValueError:
            continue
        finally:
            tmp.unlink(missing_ok=True)
        return text, attempt
    raise RuntimeError(f"{case.case_id} profile {idx}: no acceptable schedule in {MAX_ATTEMPTS} attempts")


def schedule_name(idx: int) -> str:
    return f"held_out_{idx:02d}"


def expected_files() -> dict[Path, str]:
    """Every committed artifact this generator owns: stim files and manifests."""
    out: dict[Path, str] = {}
    for case in CASES:
        manifest_path = case.directory / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["schedules"] = {k: v for k, v in manifest["schedules"].items() if not v.get("held_out")}
        stim_list = [s for s in manifest["analysis_inputs"]["stimulus"] if "/held_out_" not in s]
        for idx in range(1, len(PROFILES[case.case_id]) + 1):
            name = schedule_name(idx)
            text, _ = generate_case(case, idx)
            rel = f"stim/{name}.stim"
            out[case.directory / rel] = text
            manifest["schedules"][name] = {"stim": rel, "held_out": True}
            stim_list.append(rel)
        manifest["analysis_inputs"]["stimulus"] = stim_list
        out[manifest_path] = json.dumps(manifest, indent=2) + "\n"
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)
    want = expected_files()
    stale = [p for p, text in want.items() if not p.exists() or p.read_text() != text]
    if args.check:
        for p in stale:
            print(f"differs: {p.relative_to(BENCH_ROOT.parent)}", file=sys.stderr)
        return 1 if stale else 0
    for p, text in want.items():
        p.write_text(text)
    print(f"wrote {len(stale)} changed file(s) of {len(want)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
