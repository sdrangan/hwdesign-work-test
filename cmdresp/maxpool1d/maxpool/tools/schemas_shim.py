"""Verbatim copy of the schema section of maxpool.py, for waveflow_validate_schema only.

The validator exec()s its input and needs the schema classes defined in the text; maxpool.py
itself cannot be exec()ed that way (a module-level @dataclass needs sys.modules).  Nothing
imports this file.
"""
from enum import IntEnum

from waveflow.hw.dataschema import DataList, EnumField, IntField

CmdTypeField = IntField.specialize(bitwidth=8, signed=False)
TxIdField = IntField.specialize(bitwidth=16, signed=False)
NField = IntField.specialize(bitwidth=16, signed=False)
KField = IntField.specialize(bitwidth=8, signed=False)


class MaxpoolError(IntEnum):
    NO_ERROR = 0
    TLAST_EARLY_SAMP_IN = 1
    NO_TLAST_SAMP_IN = 2
    BAD_PARAM = 3
    UNKNOWN_CMD = 4
    TLAST_EARLY_HDR = 5
    NO_TLAST_HDR = 6

MaxpoolErrorField = EnumField.specialize(enum_type=MaxpoolError)


class MaxpoolCmdHdr(DataList):
    elements = {
        "cmd_type": {"schema": CmdTypeField, "description": "COMPUTE = 0, END = 1; others unknown"},
        "tx_id":    {"schema": TxIdField,    "description": "Command ID: echoed, or reported on error"},
        "n":        {"schema": NField,       "description": "Signal length, 1..128 (0 for END)"},
        "k":        {"schema": KField,       "description": "Window size, 1..min(n, 8) (0 for END)"},
    }


class MaxpoolRespHdr(DataList):
    elements = {
        "tx_id": {"schema": TxIdField, "description": "Echo of the transaction ID"},
    }
