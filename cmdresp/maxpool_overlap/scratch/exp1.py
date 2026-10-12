import numpy as np
from enum import IntEnum
from waveflow.hw.dataschema import DataList, EnumField, IntField
from waveflow.hw.arrayutils import write_array, read_array
I16 = IntField.specialize(bitwidth=16, signed=True, include_dir="include")
print(I16, getattr(I16, '__name__', None))
w = write_array(np.array([1, -2, 3], dtype=np.int16), elem_type=I16, word_bw=32)
print([hex(int(x)) for x in w], w.dtype)
print(read_array(w, elem_type=I16, word_bw=32, shape=3).val)
U8 = IntField.specialize(bitwidth=8, signed=False)
U16 = IntField.specialize(bitwidth=16, signed=False)


class H(DataList):
    elements = {"cmd_type": {"schema": U8}, "tx_id": {"schema": U16}, "n0": {"schema": U16},
                "n1": {"schema": U16}, "s0": {"schema": U8}, "s1": {"schema": U8},
                "k0": {"schema": U8}, "k1": {"schema": U8}}


h = H()
h.cmd_type = 7; h.tx_id = 0xabcd; h.n0 = 0x101; h.n1 = 0x102; h.s0 = 1; h.s1 = 2; h.k0 = 3; h.k1 = 4
print([hex(int(x)) for x in h.serialize(word_bw=32)])


class E(IntEnum):
    A = 0
    B = 1


EF = EnumField.specialize(enum_type=E)
print(vars(EF).get('bitwidth'))
