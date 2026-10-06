# Schema section of fir.py, extracted verbatim for waveflow_validate_schema.
from enum import IntEnum
from waveflow.hw.dataschema import DataList, EnumField, IntField
# ---
# 1. Schemas
# ---------------------------------------------------------------------------

INCLUDE_DIR = "include"
WORD_BW_SUPPORTED = [32, 64]
NTAPS_MAX = 32                     # taps, and so the delay line holds NTAPS_MAX - 1 samples
ACC_BITS = 40                      # accumulator width in the C++ (>= 37 required)
NERR_MAX = (1 << 16) - 1           # nerr saturates here

TxIdField   = IntField.specialize(bitwidth=16, signed=False)
OpcodeField = IntField.specialize(bitwidth=8,  signed=False)
CountField  = IntField.specialize(bitwidth=16, signed=False)
DecimField  = IntField.specialize(bitwidth=8,  signed=False)
ResetField  = IntField.specialize(bitwidth=1,  signed=False)
NtapsField  = IntField.specialize(bitwidth=8,  signed=False)
NsampTotalField = IntField.specialize(bitwidth=32, signed=False)
#: One Q1.15 value: a sample x, a tap h or an output y.
Int16 = IntField.specialize(bitwidth=16, signed=True, include_dir=INCLUDE_DIR)


class FirOpcode(IntEnum):
    """The defined opcodes.  The header field itself is a plain u8, because any other value
    must reach the kernel to be reported as ``BAD_OPCODE``."""
    LOAD_TAPS = 1
    PROCESS = 2
    STATUS = 3


class FirError(IntEnum):
    NO_ERROR = 0
    BAD_OPCODE = 1
    BAD_NTAPS = 2
    NO_TAPS = 3
    BAD_DECIM = 4
    TLAST_EARLY = 5
    NO_TLAST = 6

FirErrorField = EnumField.specialize(enum_type=FirError)


class FirCmdHdr(DataList):
    """Command header, the first burst of every command."""
    elements = {
        "tx_id":  {"schema": TxIdField,   "description": "Transaction ID"},
        "opcode": {"schema": OpcodeField, "description": "1 LOAD_TAPS, 2 PROCESS, 3 STATUS"},
        "count":  {"schema": CountField,  "description": "ntaps (LOAD_TAPS) or nsamp (PROCESS)"},
        "decim":  {"schema": DecimField,  "description": "Decimation factor D (PROCESS only)"},
        "reset":  {"schema": ResetField,  "description": "1 = zero the delay line first (PROCESS only)"},
    }


class FirRespHdr(DataList):
    """Response header: the command's tx_id and opcode, copied."""
    elements = {
        "tx_id":  {"schema": TxIdField,   "description": "Copied from the command"},
        "opcode": {"schema": OpcodeField, "description": "Copied from the command"},
    }


class FirRespFtr(DataList):
    """Response footer, the last burst of every response."""
    elements = {
        "nin":   {"schema": CountField,    "description": "Input values consumed from the payload"},
        "nout":  {"schema": CountField,    "description": "Output samples written"},
        "error": {"schema": FirErrorField, "description": "Error code of this command"},
    }


class FirStatusMsg(DataList):
    """The STATUS command's message."""
    elements = {
        "ntaps":       {"schema": NtapsField,      "description": "Loaded taps, 0 if none"},
        "nsamp_total": {"schema": NsampTotalField, "description": "Samples filtered since the last successful LOAD_TAPS"},
        "nerr":        {"schema": TxIdField,       "description": "Errors since power-up, saturating"},
        "last_error":  {"schema": FirErrorField,   "description": "Most recent error"},
    }


SCHEMA_CLASSES = [
    FirErrorField,
    FirCmdHdr,
    FirRespHdr,
    FirRespFtr,
    FirStatusMsg,
]

