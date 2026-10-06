"""quick_synth.py -- C synthesis only, one width, for timing/resource iterations.

    python quick_synth.py 32        # project prj_csynth_w32

Prints the estimated clock, the loop II and the resources.  The numbers results.md reports
come from the full build (fir_build.py), not from this script.
"""
from __future__ import annotations

import sys
from pathlib import Path

from waveflow.toolchain import toolchain

from fir_build import TOPS, _parse_csynth_xml

ROOT = Path(__file__).resolve().parent


def main() -> None:
    bw = int(sys.argv[1])
    env = {"WAVEFLOW_FIR_STAGE": "csynth", "WAVEFLOW_FIR_BW": str(bw),
           "WAVEFLOW_FIR_TOP": TOPS[bw], "WAVEFLOW_FIR_CLK_PERIOD_NS": "10"}
    r = toolchain.run_vitis_hls(ROOT / "run.tcl", work_dir=ROOT, capture_output=True, env=env)
    for line in (r.stdout or "").splitlines():
        if any(k in line for k in ("HLS 200-871", "HLS 200-887", "HLS 200-885", "II Violation",
                                   "Estimated Fmax", "WAVEFLOW")):
            print(line)
    print(_parse_csynth_xml(ROOT / f"prj_csynth_w{bw}" / "solution1" / "syn" / "report" / "csynth.xml"))


if __name__ == "__main__":
    main()
