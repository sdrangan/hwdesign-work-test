"""Print the wire layout by serializing instances (the source of layout.md)."""
import numpy as np

from maxpool import Int16, MaxpoolCmdHdr, MaxpoolRespHdr, maxpool_eval
from waveflow.hw.arrayutils import read_array, write_array


def show(name, words):
    print(f"{name}: {[f'0x{int(w):08x}' for w in words]}")


for field, val in (("cmd_type", 0xAA), ("n", 0x1234), ("k", 0x5678), ("tx_id", 0xBEEF)):
    h = MaxpoolCmdHdr()
    setattr(h, field, val)
    show(f"CmdHdr only {field}=0x{val:X}", h.serialize(word_bw=32))
h = MaxpoolCmdHdr()
h.cmd_type, h.n, h.k, h.tx_id = 0, 10, 3, 7
show("CmdHdr COMPUTE n=10 k=3 tx_id=7", h.serialize(word_bw=32))
r = MaxpoolRespHdr()
r.tx_id = 0xBEEF
show("RespHdr tx_id=0xBEEF", r.serialize(word_bw=32))
show("int16 [1, -2, 3]", write_array(np.array([1, -2, 3], np.int16), elem_type=Int16, word_bw=32))
print("read 0xfffe0001 ->", read_array([0xFFFE0001], elem_type=Int16, word_bw=32, shape=2).val)
h2 = MaxpoolCmdHdr().deserialize([0x7, 0], word_bw=32)
print("unknown code deserializes:", h2.cmd_type, type(h2.cmd_type))
print(maxpool_eval([1, 5, 2, 7, 3, 0, 9], 3), maxpool_eval([4], 1))
