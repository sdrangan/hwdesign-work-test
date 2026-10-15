"""checker_demo.py -- shows scenarios.check rejecting wrong outputs.

Each wrong output is written as a stage of its own under data/w32/<scenario>/, then checked:

- ``wrong_round``  rand_1024 with the right function but rounding by truncation (p >> 14,
                   no + 2^13);
- ``no_close``     early_tlast with the error path's last LLR word missing its TLAST;
- ``no_halt``      early_tlast with the right words but a status that does not halt.

    python checker_demo.py          # needs the scenarios written; writes results/checker_demo.txt
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

import scenarios as S
from qam16_llr import Int8
from waveflow.hw.arrayutils import write_array
from waveflow.utils.burst_io import StreamBurst, read_bursts, write_bursts

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data" / "w32"


def _truncating_eval(inv_var: int, yi: np.ndarray, yq: np.ndarray) -> np.ndarray:
    out = []
    for y in (yi.astype(np.int64), yq.astype(np.int64)):
        for p in (inv_var * y, inv_var * (512 - np.abs(y))):
            out.append(np.clip(p >> 14, -128, 127))
    return np.stack(out, axis=-1)


def main() -> int:
    lines = []
    sc = S.scenarios()

    # wrong_round: the expected header, then LLRs rounded by truncation.
    t = sc["rand_1024"].txs[0]
    want = read_bursts(DATA / "rand_1024" / "expected")
    llr = _truncating_eval(t.inv_var, t.yi, t.yq).reshape(-1).astype(np.int8)
    bad = [want[0], StreamBurst(write_array(llr, elem_type=Int8, word_bw=32), True)]
    write_bursts(bad, DATA / "rand_1024" / "wrong_round")
    (DATA / "rand_1024" / "wrong_round" / "status.json").write_text(
        (DATA / "rand_1024" / "expected_status.json").read_text())

    # no_close and no_halt, from early_tlast's expected output.
    want = read_bursts(DATA / "early_tlast" / "expected")
    st = json.loads((DATA / "early_tlast" / "expected_status.json").read_text())
    write_bursts(want[:-1] + [StreamBurst(want[-1].words, False)], DATA / "early_tlast" / "no_close")
    (DATA / "early_tlast" / "no_close" / "status.json").write_text(json.dumps(st))
    write_bursts(want, DATA / "early_tlast" / "no_halt")
    (DATA / "early_tlast" / "no_halt" / "status.json").write_text(
        json.dumps({"halted": 0, "error": 0, "tx_id": 0}))

    rejected = 0
    for name, stage in (("rand_1024", "wrong_round"), ("early_tlast", "no_close"),
                        ("early_tlast", "no_halt")):
        problems = S.check(DATA, stage, [name])[name]
        rejected += bool(problems)
        lines.append(f"{stage:12s} on {name:12s}: {'REJECTED' if problems else 'ACCEPTED (bad)'}")
        lines += [f"    {p}" for p in problems]
    ok = rejected == 3
    lines.append(f"OVERALL {'PASS' if ok else 'FAIL'}: the checker rejected {rejected} of 3 wrong outputs")
    text = "\n".join(lines) + "\n"
    (ROOT / "results").mkdir(exist_ok=True)
    (ROOT / "results" / "checker_demo.txt").write_text(text, encoding="utf-8")
    print(text, end="")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
