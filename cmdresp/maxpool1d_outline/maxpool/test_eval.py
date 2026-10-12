"""test_eval.py -- Stage 1 checks: pin down maxpool_eval, and show the checker rejecting.

    python test_eval.py <data_dir>      (data_dir: data/w32, after the `scenarios` step)

1. maxpool_eval against three hand-computed examples.
2. maxpool_eval against the spec's NumPy reference on random signals, every (n, k) at the
   limits and a random sweep:  y = x[:n // k * k].reshape(n // k, k).max(axis=1)
3. The checker rejecting deliberately wrong outputs, written as fake stages.

Exits non-zero if any example or reference comparison fails, or if the checker accepts a
wrong output.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import numpy as np

import scenarios as S
from maxpool import Int16, MaxpoolError, maxpool_eval
from waveflow.hw.arrayutils import write_array
from waveflow.utils.burst_io import StreamBurst, read_bursts, write_bursts

failures = 0

# 1. Hand-computed examples.
HAND = [
    # x, k, y (worked by hand)
    ([3, -1, 4, 1, -5, 9, 2, 6], 2, [3, 4, 9, 6]),        # max(3,-1)=3 max(4,1)=4 max(-5,9)=9 max(2,6)=6
    ([1, 5, 2, 7, 3, 0, 9], 3, [5, 7]),                    # (1,5,2)->5 (7,3,0)->7; 9 is past the last whole window
    ([-32768, -7, -32768, -32768, 100], 4, [-7]),          # all-negative window ->-7; 100 ignored
]
print("1. hand-computed examples")
for x, k, want in HAND:
    got = maxpool_eval(x, k).tolist()
    ok = got == want
    failures += not ok
    print(f"   x={x} k={k}: maxpool_eval={got} hand={want} {'PASS' if ok else 'FAIL'}")

# 2. The NumPy reference.
print("2. NumPy reference: y = x[:n // k * k].reshape(n // k, k).max(axis=1)")
rng = np.random.default_rng(7)
cases = [(n, k) for n in (1, 2, 7, 8, 9, 127, 128) for k in range(1, min(n, 8) + 1)]
cases += [(int(rng.integers(1, 129)), 0) for _ in range(300)]
ncmp = 0
for n, k in cases:
    if k == 0:
        k = int(rng.integers(1, min(n, 8) + 1))
    x = rng.integers(-32768, 32768, n).astype(np.int16)
    ref = x[:n // k * k].reshape(n // k, k).max(axis=1)
    got = maxpool_eval(x, k)
    if got.dtype != np.int16 or not np.array_equal(got, ref):
        failures += 1
        print(f"   FAIL n={n} k={k}: {got} vs {ref}")
    ncmp += 1
print(f"   {ncmp} random signals (all (n, k) at n in 1,2,7,8,9,127,128, plus 300 random): "
      f"{'PASS' if failures == 0 else 'FAIL'}")

# 3. The checker rejecting wrong outputs.
print("3. checker rejecting wrong outputs")
data = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "data" / "w32"


def fake(name: str, stage: str, bursts: list[StreamBurst], status: dict) -> list[str]:
    d = data / name / stage
    shutil.rmtree(d, ignore_errors=True)
    write_bursts(bursts, d)
    (d / "status.json").write_text(json.dumps(status) + "\n", encoding="utf-8")
    problems = S.check(data, stage, [name])[name]
    shutil.rmtree(d, ignore_errors=True)
    return problems


def int16_words(y) -> np.ndarray:
    return write_array(np.asarray(y, np.int16), elem_type=Int16, word_bw=32)


wrongs = []
# (a) A partial last window emitted (ceil(n/k) windows instead of floor): rem_n10_k3.
sc = S.scenarios()["rem_n10_k3"]
t = sc.txs[0]
y_bad = [int(t.samples[i:i + 3].max()) for i in range(0, 10, 3)]          # 4 windows, the last partial
exp, st = S.expected(sc)
wrongs.append(("partial window emitted (rem_n10_k3)", "rem_n10_k3",
               [exp[0], StreamBurst(int16_words(y_bad))], st))
# (b) An early TLAST on the signal that does not halt: err_sig_early with a clean status.
exp, st = S.expected(S.scenarios()["err_sig_early"])
wrongs.append(("early TLAST not halting (err_sig_early)", "err_sig_early",
               exp, {"halted": 0, "error": 0, "tx_id": 0}))
# (c) The y burst not closed with TLAST at the error: err_sig_early_held.
exp, st = S.expected(S.scenarios()["err_sig_early_held"])
wrongs.append(("burst left open at the error (err_sig_early_held)", "err_sig_early_held",
               [exp[0], StreamBurst(exp[1].words, False)], st))
# (d) Wrong precedence: unknown command reported instead of the missing header TLAST.
exp, st = S.expected(S.scenarios()["err_prec_missing_cmd"])
wrongs.append(("precedence: UNKNOWN_CMD over NO_TLAST_HDR (err_prec_missing_cmd)",
               "err_prec_missing_cmd", exp, {**st, "error": int(MaxpoolError.UNKNOWN_CMD)}))
# (e) tx_id reported although the header ended before it arrived.
exp, st = S.expected(S.scenarios()["err_prec_early_cmd"])
wrongs.append(("tx_id reported from an undelivered word (err_prec_early_cmd)",
               "err_prec_early_cmd", exp, {**st, "tx_id": 53}))
for label, name, bursts, status in wrongs:
    problems = fake(name, "wrong", bursts, status)
    print(f"   {label}: {'REJECTED' if problems else 'ACCEPTED (checker bug)'}")
    for p in problems:
        print(f"       {p}")
    failures += not problems

print("RESULT:", "PASS" if failures == 0 else f"FAIL ({failures})")
sys.exit(1 if failures else 0)
