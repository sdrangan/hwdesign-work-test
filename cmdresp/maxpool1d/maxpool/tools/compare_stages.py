"""Direct bit-for-bit comparison of the model, csim and cosim recordings of every scenario:
every file each stage recorded (words, burst boundaries/TLAST, status) must be byte-identical.

    python tools/compare_stages.py
"""
import json
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data" / "w32"

names = (DATA / "scenarios.txt").read_text(encoding="utf-8").split()
bad = 0
for name in names:
    ref = DATA / name / "model"
    files = sorted(p.name for p in ref.iterdir())
    for stage in ("csim", "cosim"):
        for f in files:
            a, b = ref / f, DATA / name / stage / f
            # meta.json is written by Python for the model and by C++ for csim/cosim, so its
            # formatting differs; compare its content.  Everything else byte for byte.
            same = (json.loads(a.read_text()) == json.loads(b.read_text())
                    if f.endswith(".json") else a.read_bytes() == b.read_bytes())
            if not same:
                bad += 1
                print(f"DIFF {name} {stage} {f}")
print(f"{len(names)} scenarios, files compared per stage: {files}; "
      f"model == csim == cosim: {'PASS' if not bad else f'FAIL ({bad} differ)'}")
