"""checker_demo.py -- show that the checker rejects wrong outputs.

    python checker_demo.py        # exit code 1 unless every wrong variant is rejected

Writes the scenarios, then for each word width runs three deliberately wrong variants of
the model through ``scenarios.check`` and records what the checker says, in
``results/checker_rejects.txt``:

- ``wrong_rounding``   -- the right filter, rounding by truncation (``acc >> 15``) instead of
                          round-half-up;
- ``y_without_tlast``  -- the right words, but the y burst has no TLAST (it runs into the
                          footer);
- ``lanes_swapped``    -- a packing bug: the two 16-bit halves of each y word's low 32 bits
                          are swapped.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

import fir
import scenarios as S
from waveflow.utils.burst_io import read_bursts, write_bursts

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"


def run_model(bw: int, stage: str, mutate_y=None) -> None:
    """Run the model over the session; ``mutate_y`` rewrites each PROCESS's y words."""
    real = fir.fir_command

    def command(state, hdr, rd, word_bw):
        out, st = real(state, hdr, rd, word_bw)
        nftr = fir.FirRespFtr().serialize(word_bw=word_bw).size
        if mutate_y and int(hdr.opcode) == fir.FirOpcode.PROCESS and len(out) > 1 + nftr:
            out = out[:1] + mutate_y(out[1:-nftr]) + out[-nftr:]   # RespHdr | y | RespFtr
        return out, st

    fir.fir_command = command
    try:
        st = fir.FirState()
        d0 = DATA / f"w{bw}"
        for name in S.scenarios():
            out, st = fir.fir_stream_model(read_bursts(d0 / name / "in"), bw, st)
            write_bursts(out, d0 / name / stage)
    finally:
        fir.fir_command = real


def y_without_tlast(y: list[tuple[int, bool]]) -> list[tuple[int, bool]]:
    return [(w, False) for w, _ in y]


def lanes_swapped(y: list[tuple[int, bool]]) -> list[tuple[int, bool]]:
    return [((w & ~0xFFFFFFFF) | ((w & 0xFFFF) << 16) | ((w >> 16) & 0xFFFF), t) for w, t in y]


def main() -> int:
    S.write_scenarios(DATA)
    lines, ok = [], True
    for bw in S.WORD_BWS:
        d0 = DATA / f"w{bw}"
        run_model(bw, "model")
        rep = S.check(d0, "model", bw)
        bad = [n for n, p in rep.items() if p]
        lines.append(f"[w{bw}] reference model: {len(rep) - len(bad)}/{len(rep)} checks pass {bad or ''}")
        ok &= not bad

        variants = {
            "wrong_rounding": dict(patch=lambda a: np.clip(np.asarray(a, np.int64) >> 15,
                                                            -32768, 32767)),
            "y_without_tlast": dict(mutate_y=y_without_tlast),
            "lanes_swapped": dict(mutate_y=lanes_swapped),
        }
        for vname, v in variants.items():
            saved = fir.fir_round_sat
            if "patch" in v:
                fir.fir_round_sat = v["patch"]
            try:
                run_model(bw, vname, v.get("mutate_y"))
            finally:
                fir.fir_round_sat = saved
            rep = S.check(d0, vname, bw)
            failed = {n: p for n, p in rep.items() if p}
            rejected = bool(failed)
            ok &= rejected
            lines.append(f"[w{bw}] {vname}: {'REJECTED' if rejected else 'NOT REJECTED'} -- "
                         f"{len(failed)}/{len(rep)} checks fail")
            for n, p in list(failed.items())[:4]:
                lines.append(f"        {n}: {p[0]}" + (f"  (+{len(p) - 1} more)" if len(p) > 1 else ""))
    text = "\n".join(lines)
    print(text)
    (ROOT / "results").mkdir(exist_ok=True)
    (ROOT / "results" / "checker_rejects.txt").write_text(text + "\n", encoding="utf-8")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
