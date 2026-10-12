"""Serialize instances with Waveflow and print what came out (for layout.md)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np  # noqa: E402

from maxpool import Int16, MaxpoolCmdHdr, MaxpoolRespHdr  # noqa: E402
from waveflow.hw.arrayutils import write_array  # noqa: E402

h = MaxpoolCmdHdr()
h.cmd_type, h.tx_id, h.n0, h.n1, h.s0, h.s1, h.k0, h.k1 = 0x5A, 0x1234, 0x0100, 0x00FF, 3, 4, 5, 6
print("CmdHdr cmd_type=0x5A tx_id=0x1234 n0=0x0100 n1=0x00FF s0=3 s1=4 k0=5 k1=6:")
for i, w in enumerate(h.serialize(word_bw=32)):
    print(f"  word {i}: 0x{int(w):08x}")
r = MaxpoolRespHdr()
r.tx_id = 0xBEEF
print("RespHdr tx_id=0xBEEF:")
for i, w in enumerate(r.serialize(word_bw=32)):
    print(f"  word {i}: 0x{int(w):08x}")
for x in ([1, -2, 3], [0x1111, 0x2222, 0x3333, 0x4444]):
    print(f"Int16 pixels {x}:")
    for i, w in enumerate(write_array(np.array(x, np.int16), elem_type=Int16, word_bw=32)):
        print(f"  word {i}: 0x{int(w):08x}")
