"""check_eval.py -- pins down qam16_llr_eval before anything downstream trusts it.

1. Three hand-computed examples (one saturating), worked out below in comments.
2. An independent reference: prompt.md's integer formulas written with Python integers
   (arbitrary precision, floor division for the arithmetic shift), applied to random samples
   over the whole int16 range and to every boundary value, compared bit for bit.
3. The stream model's parameter checks on headers no scenario sends (nsamp = 0, 65535).

    python check_eval.py            # writes results/eval_check.txt; exit 1 on any failure
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

from qam16_llr import (
    Qam16LlrCmdHdr, Qam16LlrCmdType, Qam16LlrError, qam16_llr_eval, qam16_llr_stream_model,
)
from waveflow.utils.burst_io import StreamBurst

# (inv_var, yI, yQ) raw -> [b0, b1, b2, b3] raw s6.2, by hand:
HAND = [
    # inv_var = 1.0 (256), yI = 1.0 (256), yQ = -0.5 (-128)
    # b0: 256*256 = 65536;          (65536 + 8192) >> 14 = floor(4.5)   =   4  (1.0)
    # b1: 512-256 = 256 -> 65536;   same                                =   4  (1.0)
    # b2: 256*-128 = -32768;        (-32768 + 8192) >> 14 = floor(-1.5) =  -2  (-0.5)
    # b3: 512-128 = 384 -> 98304;   (98304 + 8192) >> 14 = floor(6.5)   =   6  (1.5)
    ("unit", 256, 256, -128, [4, 4, -2, 6]),
    # inv_var = 2.5 (640), yI = 0.75 (192), yQ = -0.75 (-192): ties in both directions
    # b0: 640*192 = 122880;         (122880 + 8192) >> 14 = 8.0         =   8  (1.875 -> 2.0)
    # b1: 512-192 = 320 -> 204800;  (204800 + 8192) >> 14 = 13.0        =  13  (3.125 -> 3.25)
    # b2: 640*-192 = -122880;       (-122880 + 8192) >> 14 = -7.0       =  -7  (-1.875 -> -1.75)
    # b3: 512-192 = 320 -> 204800;                                      =  13
    ("rounding", 640, 192, -192, [8, 13, -7, 13]),
    # inv_var = 0xFFFF, yI = 32767, yQ = -32768: everything saturates
    # b0: 65535*32767 = 2147385345  >> 14 ~ 131066         -> 127
    # b1: 512-32767 = -32255 -> -2113831425 >> 14 ~ -129018 -> -128
    # b2: 65535*-32768 = -2147450880 ...                    -> -128
    # b3: 512-32768 = -32256 -> -2113896960 ...             -> -128
    ("saturate", 0xFFFF, 32767, -32768, [127, -128, -128, -128]),
]


def reference(inv_var: int, yi: int, yq: int) -> list[int]:
    """prompt.md's formulas, one sample, plain Python integers."""
    def rnd(p: int) -> int:
        return max(-128, min(127, (p + 2 ** 13) // 2 ** 14))
    return [rnd(inv_var * yi), rnd(inv_var * (512 - abs(yi))),
            rnd(inv_var * yq), rnd(inv_var * (512 - abs(yq)))]


def main() -> int:
    lines: list[str] = []
    ok = True

    lines.append("1. Hand-computed examples (inv_var, yI, yQ raw -> b0..b3 raw s6.2)")
    for name, iv, yi, yq, want in HAND:
        got = qam16_llr_eval(iv, [yi], [yq])[0].tolist()
        good = got == want
        ok &= good
        lines.append(f"   {name:9s} inv_var={iv:#06x} yI={yi:6d} yQ={yq:6d} -> {got} "
                     f"expected {want}  {'PASS' if good else 'FAIL'}")

    lines.append("2. Independent reference (Python integers) vs qam16_llr_eval, bit for bit")
    rng = np.random.default_rng(7)
    edges = np.array([-32768, -32767, -513, -512, -511, -1, 0, 1, 511, 512, 513, 32767])
    for iv in [0, 1, 0x0100, 0x0155, 0x0800, 0x7FFF, 0xFFFF, *rng.integers(0, 65536, 5).tolist()]:
        yi = np.concatenate([rng.integers(-32768, 32768, 4000), np.repeat(edges, len(edges))])
        yq = np.concatenate([rng.integers(-32768, 32768, 4000), np.tile(edges, len(edges))])
        got = qam16_llr_eval(int(iv), yi, yq)
        want = np.array([reference(int(iv), int(a), int(b)) for a, b in zip(yi, yq)])
        nbad = int(np.sum(np.any(got != want, axis=1)))
        nsat = int(np.sum((want == 127) | (want == -128)))
        ok &= nbad == 0
        lines.append(f"   inv_var={int(iv):#06x}: {len(yi)} samples, {nsat} saturated LLRs, "
                     f"{nbad} mismatches  {'PASS' if nbad == 0 else 'FAIL'}")

    lines.append("3. Stream model: nsamp outside 1..1024 halts with BAD_PARAM, nothing written")
    for nsamp in (0, 1025, 65535):
        h = Qam16LlrCmdHdr()
        h.cmd_type, h.tx_id, h.nsamp, h.inv_var = int(Qam16LlrCmdType.DEMAP), 9, nsamp, 256
        res = qam16_llr_stream_model([StreamBurst(h.serialize(word_bw=32))])
        good = res.out == [] and res.status() == {"halted": 1, "error": int(Qam16LlrError.BAD_PARAM),
                                                  "tx_id": 9}
        ok &= good
        lines.append(f"   nsamp={nsamp:5d}: status {res.status()}, {len(res.out)} bursts  "
                     f"{'PASS' if good else 'FAIL'}")

    lines.append(f"OVERALL {'PASS' if ok else 'FAIL'}")
    text = "\n".join(lines) + "\n"
    out = Path(__file__).resolve().parent / "results" / "eval_check.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
