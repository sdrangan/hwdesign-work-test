"""Shows the checker rejecting wrong outputs: each is a plausible kernel bug, built by
corrupting the model's correct output for one scenario, then run through scenarios.check.

    python tests/demo_checker_rejects.py      (after `maxpool_build.py --through scenarios`)
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import scenarios as S  # noqa: E402
from maxpool import Int16, maxpool_stream_model  # noqa: E402
from waveflow.hw.arrayutils import write_array  # noqa: E402
from waveflow.utils.burst_io import StreamBurst, read_bursts, write_bursts  # noqa: E402

DATA = ROOT / "data" / "w32"


def pool_with_tail(x: np.ndarray, k: int) -> np.ndarray:
    """Wrong function: also pools the partial window at the end."""
    return np.array([x[i:i + k].max() for i in range(0, len(x), k)], np.int16)


def run(name: str, scenario: str, out: list[StreamBurst], status: dict) -> None:
    d = DATA / scenario / "wrong"
    shutil.rmtree(d, ignore_errors=True)
    write_bursts(out, d)
    (d / "status.json").write_text(json.dumps(status) + "\n", encoding="utf-8")
    problems = S.check(DATA, "wrong", [scenario])[scenario]
    print(f"{name}  [{scenario}]: {'REJECTED' if problems else 'accepted (!)'}")
    for p in problems:
        print(f"    {p}")
    shutil.rmtree(d)
    assert problems, f"the checker accepted a wrong output: {name}"


def main() -> None:
    # 1. The tail is pooled too (y has ceil(n/k) entries).  'worked', command 2: n=8, k=3.
    res = maxpool_stream_model(read_bursts(DATA / "worked" / "in"))
    out = list(res.out)
    x2 = np.array([-7, -3, -9, -2, -8, -1, 0, 5], np.int16)
    out[3] = StreamBurst(write_array(pool_with_tail(x2, 3), elem_type=Int16, word_bw=32), True)
    run("1. partial window pooled", "worked", out, res.status())

    # 2. The error path does not close its output burst: TLAST dropped on the last result.
    res = maxpool_stream_model(read_bursts(DATA / "err_sig_early_tlast" / "in"))
    out = list(res.out)
    out[-1] = StreamBurst(out[-1].words, False)
    run("2. early-TLAST burst not closed", "err_sig_early_tlast", out, res.status())

    # 3. An error path that does not halt: right words, status left clear.
    res = maxpool_stream_model(read_bursts(DATA / "err_k_gt_n" / "in"))
    run("3. BAD_PARAM without halting", "err_k_gt_n", list(res.out),
        {"halted": 0, "error": 0, "tx_id": 0})

    # 4. BAD_PARAM checked too late: a response header written before the check.
    res = maxpool_stream_model(read_bursts(DATA / "err_n_too_big" / "in"))
    out = list(res.out) + [res.out[0]]
    run("4. response header written for a bad command", "err_n_too_big", out, res.status())

    # 5. Status not cleared at start (rule 5): the poisoned registers left behind.
    res = maxpool_stream_model(read_bursts(DATA / "limits" / "in"))
    run("5. status not cleared", "limits", list(res.out),
        {"halted": 1, "error": 2, "tx_id": 65535})


if __name__ == "__main__":
    main()
