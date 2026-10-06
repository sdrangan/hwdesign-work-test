"""worked_examples.py -- hand-computed values that pin down fir_eval.

    python worked_examples.py      # prints every example; exit code 1 on any mismatch

Every expected value below was computed by hand from the spec,
``acc = sum h[k] x[n-k]``, ``y = sat16((acc + 2^14) >> 15)``, and is written with the
arithmetic that produced it.  Writes ``results/worked_examples.txt``.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

from fir import NTAPS_MAX, fir_eval

Q = 1 << 15          # 32768
HALF = 1 << 14       # 16384

# (name, taps, x, hist or None, expected y, how it was computed)
EXAMPLES = [
    ("unity-ish gain", [32767], [16384], None, [16384],
     "acc = 32767*16384 = 536854528; +16384 = 536870912 = 2^29; >>15 = 16384"),
    ("exactly +1/2 rounds up", [1], [16384], None, [1],
     "acc = 16384; +16384 = 32768; >>15 = 1   (0.5 LSB -> 1)"),
    ("exactly -1/2 rounds up", [1], [-16384], None, [0],
     "acc = -16384; +16384 = 0; >>15 = 0     (-0.5 LSB -> 0, not -1)"),
    ("just below +1/2", [1], [16383], None, [0],
     "acc = 16383; +16384 = 32767; >>15 = 0"),
    ("just below -1/2: arithmetic shift", [1], [-16385], None, [-1],
     "acc = -16385; +16384 = -1; >>15 = -1   (floor, not truncation toward 0)"),
    ("-1.5 LSB rounds up to -1", [1], [-49152], None, [-1],
     "acc = -49152; +16384 = -32768; >>15 = -1"),
    ("saturate positive", [32767, 32767], [32767, 32767], None, [32766, 32767],
     "n=0: 32767^2 = 1073676289; +16384 = 1073692673; >>15 = 32766. "
     "n=1: 2*1073676289 = 2147352578; +16384 >>15 = 65532 -> clip 32767"),
    ("saturate negative", [-32768, -32768], [32767, 32767], None, [-32767, -32768],
     "n=0: -32768*32767 = -1073709056; +16384 >>15 = floor(-32766.5) = -32767. "
     "n=1: 2x = -2147418112; +16384 >>15 = floor(-65533.5) = -65534 -> clip -32768"),
    ("(-1)*(-1) overflows", [-32768], [-32768], None, [32767],
     "acc = 2^30; +16384 >>15 = 32768 -> clip 32767"),
    ("two-tap average, delay line from zero", [16384, 16384], [100, 201, -301], None, [50, 151, -50],
     "n=0: 16384*100 = 1638400 (=50.0 Q); +16384 >>15 = 50. "
     "n=1: 16384*301 = 4931584 (=150.5); +16384 >>15 = 151. "
     "n=2: 16384*(-100) = -1638400 (=-50.0); >>15 after +16384 = -50"),
    ("delay line carried in from an earlier command", [16384, 16384], [201, -301],
     [0] * (NTAPS_MAX - 2) + [100], [151, -50],
     "same as the previous example from n=1: hist ends in x[-1] = 100"),
    ("32 taps, 37-bit accumulator, positive", [-32768] * 32, [-32768] * 32, None,
     [32767] * 32,
     "n=31: acc = 32*2^30 = 2^35 (needs 37 signed bits) -> clip 32767; "
     "n=0: 2^30 -> 32768 -> clip 32767"),
    ("32 taps, negative full scale", [32767] * 32, [-32768] * 32, None,
     [-32767] + [-32768] * 31,
     "n=0: -32768*32767 = -1073709056 -> floor(-32766.5) = -32767; n>=1: <= -65534 -> clip -32768"),
    ("pure delay: h = [0, 0, 0.5]", [0, 0, 16384], [1000, -2000, 3000, 4], None,
     [0, 0, 500, -1000],
     "y[n] = x[n-2]/2: 0, 0, 1000/2 = 500, -2000/2 = -1000 (exact)"),
]


def main() -> int:
    lines = []
    bad = 0
    for name, h, x, hist, want, how in EXAMPLES:
        got, _ = fir_eval(h, x, hist)
        ok = list(map(int, got)) == want
        bad += not ok
        lines.append(f"{'PASS' if ok else 'FAIL'}  {name}\n"
                     f"      h={h if len(h) < 5 else f'[{h[0]}]*{len(h)}'} "
                     f"x={x if len(x) < 5 else f'[{x[0]}]*{len(x)}'}\n"
                     f"      expected {want if len(want) < 5 else want[:3] + ['...']}"
                     f"  got {list(map(int, got)) if len(got) < 5 else list(map(int, got[:3])) + ['...']}\n"
                     f"      {how}")
    # Decimation is the caller's: y[n] for n = 0, D, 2D, ...
    y, _ = fir_eval([16384, 16384], [100, 201, -301, 7])
    dec = list(map(int, y[::2]))
    ok = dec == [50, -50]
    bad += not ok
    lines.append(f"{'PASS' if ok else 'FAIL'}  decimation D=2 keeps n = 0, 2: expected [50, -50] got {dec}")
    lines.append(f"\n{len(EXAMPLES) + 1 - bad}/{len(EXAMPLES) + 1} worked examples pass")
    text = "\n".join(lines)
    print(text)
    out = Path(__file__).resolve().parent / "results" / "worked_examples.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text + "\n", encoding="utf-8")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
