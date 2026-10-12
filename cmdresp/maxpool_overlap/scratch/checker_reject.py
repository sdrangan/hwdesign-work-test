"""Stage 1: show the checker rejecting wrong outputs, and accepting the expected one."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import numpy as np  # noqa: E402

import scenarios as S  # noqa: E402
from maxpool import Geometry  # noqa: E402
from waveflow.utils.burst_io import StreamBurst, read_bursts, write_bursts  # noqa: E402

data = ROOT / "scratch" / "w32"
S.write_scenarios(data)


def put(name, stage, bursts, status):
    d = data / name / stage
    write_bursts(bursts, d)
    (d / "status.json").write_text(json.dumps(status) + "\n", encoding="utf-8")


def show(name, stage, what):
    print(f"[{what}] {name}/{stage}: {S.check(data, stage, [name])[name] or 'PASS'}")


# 0. The expected response itself passes.
for n in ("worked_examples", "err_img_early"):
    put(n, "copy", read_bursts(data / n / "expected"),
        json.loads((data / n / "expected_status.json").read_text()))
    show(n, "copy", "expected, copied")

# 1. Right max, wrong padding: windows past the edge ignore the zeros (pad with -32768).
def maxpool_no_zero_pad(x, g: Geometry):
    xp = np.full((g.n0 + g.k0, g.n1 + g.k1), -32768, np.int16)
    xp[:g.n0, :g.n1] = x
    return np.array([[xp[g.s0*i:g.s0*i+g.k0, g.s1*j:g.s1*j+g.k1].max() for j in range(g.m1)]
                     for i in range(g.m0)], np.int16)

sc = S.scenarios()["worked_examples"]
bursts, status = [], {"halted": 0, "error": 0, "tx_id": 0}
for c in sc.cmds:
    bursts.append(read_bursts(data / "worked_examples" / "expected")[len(bursts)])
    bursts.append(StreamBurst(S._pixels(maxpool_no_zero_pad(c.x, c.g))))
put("worked_examples", "wrong_padding", bursts, status)
show("worked_examples", "wrong_padding", "no zero padding")

# 2. An error path that does not halt: right words, status left clear.
exp = read_bursts(data / "err_img_early" / "expected")
put("err_img_early", "no_halt", exp, {"halted": 0, "error": 0, "tx_id": 0})
show("err_img_early", "no_halt", "error path does not halt")

# 3. The burst in progress at the error is not closed with TLAST.
open_last = exp[:-1] + [StreamBurst(exp[-1].words, False)]
put("err_img_early", "unclosed", open_last,
    json.loads((data / "err_img_early" / "expected_status.json").read_text()))
show("err_img_early", "unclosed", "burst not closed")

# 4. Early TLAST answered with all of Y (the kernel kept going).
full = S.maxpool_eval(sc_x := S.scenarios()["err_img_early"].cmds[1].x,
                      S.scenarios()["err_img_early"].cmds[1].g).ravel()
put("err_img_early", "all_of_y", exp[:-1] + [StreamBurst(S._pixels(full))],
    json.loads((data / "err_img_early" / "expected_status.json").read_text()))
show("err_img_early", "all_of_y", "early TLAST, all of Y sent")
