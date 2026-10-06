"""quick_impl.py -- Vivado place and route of the csynth project for one width.

    python quick_synth.py 32 && python quick_impl.py 32

Copies the post-route report to results/reports/w<bw>_impl_export.rpt.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

from waveflow.toolchain import toolchain

from fir_build import TOPS

ROOT = Path(__file__).resolve().parent


def main() -> None:
    bw = int(sys.argv[1])
    r = toolchain.run_vitis_hls(ROOT / "impl.tcl", work_dir=ROOT, capture_output=True,
                                env={"WAVEFLOW_FIR_BW": str(bw)})
    log = ROOT / "results" / "logs" / f"vivado_impl_w{bw}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(r.stdout or "", encoding="utf-8")
    rpt = ROOT / f"prj_csynth_w{bw}" / "solution1" / "impl" / "report" / "verilog" / f"{TOPS[bw]}_export.rpt"
    dst = ROOT / "results" / "reports" / f"w{bw}_impl_export.rpt"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(rpt, dst)
    print(dst.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
