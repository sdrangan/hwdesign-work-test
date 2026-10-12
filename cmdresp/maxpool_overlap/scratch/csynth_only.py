"""Synthesis alone (no cosim), for timing closure: prints II, estimated clock, violations."""
import re
import sys
from pathlib import Path

from waveflow.toolchain import toolchain

root = Path(__file__).resolve().parents[1]
try:
    res = toolchain.run_vitis_hls(root / "run.tcl", work_dir=root, capture_output=True,
                                  env={"WAVEFLOW_MAXPOOL_STAGE": "csynth"})
    out = res.stdout or ""
except Exception as exc:  # noqa: BLE001
    out = getattr(exc, "stdout", "") or str(exc)
(root / "scratch" / "csynth_only.log").write_text(out, encoding="utf-8")
for line in out.splitlines():
    if re.search(r"Final II|Estimated clock|Cannot meet|WAVEFLOW|ERROR", line):
        print(line[:400])
log = root / "w32_proj" / "solution1" / "solution1.log"
if log.exists():
    text = log.read_text(errors="ignore")
    m = re.search(r"critical path in module 'maxpool_Pipeline_PIXELS'.*?\n\n", text, re.S)
    if m and "-v" in sys.argv:
        print(m.group(0)[:4000])
