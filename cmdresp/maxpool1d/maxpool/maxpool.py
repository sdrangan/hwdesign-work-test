"""maxpool.py -- the streaming 1-D max-pool accelerator, written hook-first.

What is in this file, in the order a reader needs it:

1. **Schemas** -- the command header (which carries ``tx_id``, ``n`` and ``k``), the response
   header and the error codes.  Waveflow generates their C++ headers and serializers; nothing
   packs words by hand.
2. **The bit-exact model** -- pure functions with no simulator in them:
   :func:`maxpool_eval` (the arithmetic: non-overlapping windows of ``k`` int16 samples, the
   max of each) and :func:`maxpool_stream_model` (the whole protocol over a stream of bursts,
   the TLAST rules and the error checks included).  This is what the C++ kernel is checked
   against, word for word.
3. **The module** -- :class:`MaxpoolAccel` declares the ports and the register map (status
   only) and names its kernel body (``cpp_body = "body"``).  Waveflow generates the kernel's
   boundary (the prototype and every interface pragma) and a single call to the hand-written
   C++ body, ``maxpool_body_impl.tpp``.  The Python :meth:`MaxpoolAccel.body` is the same
   kernel for pysim: a thin port wrapper around :func:`maxpool_eval`.  It is functional only:
   by instruction, no timing model is built in Python; every timing number comes from RTL.
4. **The pysim testbench** -- :class:`MaxpoolTB` plays a scenario's stimulus file into the
   module, the same file the hand-written C++ testbench (``maxpool_tb.cpp``) plays into the
   Vitis kernel.

The scenarios, their independently computed expected outputs, and the checker are in
``scenarios.py``; the build that runs everything is ``maxpool_build.py``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import ClassVar

import numpy as np
import numpy.typing as npt

from waveflow.hw.arrayutils import array, read_array, write_array
from waveflow.hw.clock import Clock
from waveflow.hw.dataschema import DataList, EnumField, IntField
from waveflow.hw.hw_hostactivated import HostActivated
from waveflow.hw.hw_module import HwParam
from waveflow.hw.interface import StreamIF, StreamIFMaster, StreamIFSlave
from waveflow.hw.irq import IrqIF, IrqIFSink
from waveflow.hw.memif import DirectMMIF, MMIFMaster
from waveflow.hw.regmap import (
    Bit,
    RegAccess,
    RegField,
    VitisRegMap,
    VitisRegMapMMIFSlave,
)
from waveflow.simulation.logger import Logger, NullLogger
from waveflow.simulation.simobj import ProcessGen, SimObj
from waveflow.simulation.simulation import Simulation
from waveflow.utils.burst_io import StreamBurst, read_bursts

# ---------------------------------------------------------------------------
# 1. Schemas
# ---------------------------------------------------------------------------

INCLUDE_DIR = "include"
#: The frame fixes the stream word at 32 bits; only that width is built.
WORD_BW_SUPPORTED = [32]

#: The limits of prompt.md: 1 <= n <= N_MAX, 1 <= k <= min(n, K_MAX).
N_MAX = 128
K_MAX = 8

CmdTypeField = IntField.specialize(bitwidth=8, signed=False)
TxIdField = IntField.specialize(bitwidth=16, signed=False)
NField = IntField.specialize(bitwidth=16, signed=False)
KField = IntField.specialize(bitwidth=8, signed=False)
Int16 = IntField.specialize(bitwidth=16, signed=True, include_dir=INCLUDE_DIR)


class MaxpoolError(IntEnum):
    """What the kernel reports in its ``error`` register when it halts."""
    NO_ERROR = 0
    TLAST_EARLY_SAMP_IN = 1   # TLAST arrived before the last signal word
    NO_TLAST_SAMP_IN = 2      # the last signal word had no TLAST
    BAD_PARAM = 3             # n or k outside the limits
    UNKNOWN_CMD = 4           # cmd_type is neither COMPUTE nor END
    TLAST_EARLY_HDR = 5       # TLAST arrived before the last command-header word
    NO_TLAST_HDR = 6          # the last command-header word had no TLAST

MaxpoolErrorField = EnumField.specialize(enum_type=MaxpoolError)


class MaxpoolCmdType(IntEnum):
    """The command codes.  ``cmd_type`` is a plain 8-bit field, not an ``EnumField``: an
    unknown command is an error the kernel must report, and an ``EnumField`` cannot carry a
    value outside its enum, so no host -- and no scenario -- could send one."""
    COMPUTE = 0
    END = 1


class MaxpoolCmdHdr(DataList):
    """Command header: everything one command needs.

    A ``COMPUTE`` command is self-contained: it depends on nothing sent before it (contract
    rule 4).  ``END`` ends the activation; its ``tx_id``, ``n`` and ``k`` are sent as zeros
    and ignored.  The header is its own burst, TLAST on its last word, and the kernel checks
    that TLAST.
    """
    elements = {
        "cmd_type": {"schema": CmdTypeField, "description": "COMPUTE = 0, END = 1; others unknown"},
        "tx_id":    {"schema": TxIdField,    "description": "Command ID: echoed, or reported on error"},
        "n":        {"schema": NField,       "description": "Signal length, 1..128 (0 for END)"},
        "k":        {"schema": KField,       "description": "Window size, 1..min(n, 8) (0 for END)"},
    }


class MaxpoolRespHdr(DataList):
    """Response header: echo of the COMPUTE command's transaction ID."""
    elements = {
        "tx_id": {"schema": TxIdField, "description": "Echo of the transaction ID"},
    }


SCHEMA_CLASSES = [
    MaxpoolErrorField,
    MaxpoolCmdHdr,
    MaxpoolRespHdr,
]

# ---------------------------------------------------------------------------
# 2. The bit-exact model (pure functions)
# ---------------------------------------------------------------------------


def params_ok(n: int, k: int) -> bool:
    """The parameter check of prompt.md: ``1 <= n <= 128`` and ``1 <= k <= min(n, 8)``."""
    return 1 <= n <= N_MAX and 1 <= k <= min(n, K_MAX)


def maxpool_eval(x: npt.ArrayLike, k: int) -> npt.NDArray[np.int16]:
    """``y[i] = max(x[k*i : k*i + k])`` for ``0 <= i < len(x) // k``, in int16.

    Written as the C++ body computes it: sample by sample (the body takes a word's two
    samples in one iteration, in order), a running maximum that restarts with the first
    sample of every window, and a result when the window's ``k``-th sample arrives.  Samples past the last whole window are read and ignored.  A maximum of
    int16 values is an int16 value, so there is no rounding or saturation.
    """
    xs = np.asarray(x, dtype=np.int16)
    if k < 1:
        raise ValueError(f"k must be >= 1, got {k}")
    y: list[int] = []
    acc = 0
    a = 0                                   # position in the current window
    for v in xs:
        v = int(v)
        acc = v if a == 0 else max(acc, v)
        a += 1
        if a == k:
            y.append(acc)
            a = 0
    return np.array(y, dtype=np.int16)


@dataclass
class MaxpoolStreamResult:
    """What the kernel does to one stream: its output bursts and its final register status."""

    out: list[StreamBurst]
    halted: int = 0
    error: int = int(MaxpoolError.NO_ERROR)
    tx_id: int = 0

    def status(self) -> dict[str, int]:
        return {"halted": self.halted, "error": self.error, "tx_id": self.tx_id}


def samples_per_word(word_bw: int) -> int:
    """int16 samples in one stream word: 2 at 32 bits."""
    return word_bw // 16


def maxpool_stream_model(bursts: list[StreamBurst], word_bw: int = 32) -> MaxpoolStreamResult:
    """One activation of the kernel, as a pure function of its input stream.

    Word by word, exactly what ``maxpool_body_impl.tpp`` does:

    - the status starts clear (rule 5);
    - read the command header word by word: a TLAST before its last word is
      ``TLAST_EARLY_HDR`` (nothing more is read); no TLAST on its last word is
      ``NO_TLAST_HDR``;
    - a ``cmd_type`` other than COMPUTE or END is ``UNKNOWN_CMD``; on ``END``, return;
    - ``n`` or ``k`` outside the limits is ``BAD_PARAM``, before anything is written for the
      command and before its signal is read;
    - write the response header, TLAST on its last word;
    - read the signal a word at a time (two samples each), computing :func:`maxpool_eval`;
      the results go out as one burst, TLAST on its last word;
    - a TLAST before the last signal word is ``TLAST_EARLY_SAMP_IN``: the windows completed
      by the samples received so far are written, the burst is closed, and nothing more is
      read.  No TLAST on the last signal word is ``NO_TLAST_SAMP_IN``, after all results.

    Every error sets ``halted``, ``error`` and ``tx_id`` (the header's ``tx_id`` field as
    received) and returns at once.  The output is split into bursts at TLAST, so it compares
    directly with what ``wf::record_stream`` records from the C++ testbench.
    """
    if word_bw not in WORD_BW_SUPPORTED:
        raise ValueError(f"word_bw must be one of {WORD_BW_SUPPORTED}, got {word_bw}")
    pf = samples_per_word(word_bw)
    words = [(int(w), i + 1 == len(b.words) and b.tlast)
             for b in bursts for i, w in enumerate(np.asarray(b.words, dtype=np.uint64))]
    hdr_words = MaxpoolCmdHdr().serialize(word_bw=word_bw).size
    out: list[tuple[int, bool]] = []
    res = MaxpoolStreamResult(out=[])
    pos = 0

    def take() -> tuple[int, bool]:
        nonlocal pos
        if pos >= len(words):
            raise ValueError("the stimulus ends early: the kernel would block")
        pos += 1
        return words[pos - 1]

    def halt(err: MaxpoolError, tx_id: int) -> None:
        res.halted, res.error, res.tx_id = 1, int(err), int(tx_id)   # int: status() -> json

    while True:
        # The command header, TLAST checked.
        got: list[int] = []
        last = False
        for j in range(hdr_words):
            w, last = take()
            got.append(w)
            if last and j + 1 < hdr_words:
                break
        hdr = MaxpoolCmdHdr().deserialize(got + [0] * (hdr_words - len(got)), word_bw=word_bw)
        if len(got) < hdr_words:
            halt(MaxpoolError.TLAST_EARLY_HDR, hdr.tx_id)
            break
        if not last:
            halt(MaxpoolError.NO_TLAST_HDR, hdr.tx_id)
            break
        if hdr.cmd_type == MaxpoolCmdType.END:
            break
        if hdr.cmd_type != MaxpoolCmdType.COMPUTE:
            halt(MaxpoolError.UNKNOWN_CMD, hdr.tx_id)
            break
        n, k = int(hdr.n), int(hdr.k)
        if not params_ok(n, k):
            halt(MaxpoolError.BAD_PARAM, hdr.tx_id)
            break

        resp = MaxpoolRespHdr()
        resp.tx_id = hdr.tx_id
        rw = resp.serialize(word_bw=word_bw)
        out += [(int(w), i + 1 == rw.size) for i, w in enumerate(rw)]

        # The signal, a word at a time.
        nwords = -(-n // pf)
        x: list[int] = []
        err = MaxpoolError.NO_ERROR
        for j in range(nwords):
            w, last = take()
            x += [int(v) for v in read_array([w], elem_type=Int16, word_bw=word_bw,
                                             shape=min(pf, n - j * pf)).val]
            final = j + 1 == nwords
            if last and not final:
                err = MaxpoolError.TLAST_EARLY_SAMP_IN
                break
            if final and not last:
                err = MaxpoolError.NO_TLAST_SAMP_IN
        y = maxpool_eval(x, k)
        if len(y):
            yw = write_array(y, elem_type=Int16, word_bw=word_bw)
            out += [(int(w), i + 1 == yw.size) for i, w in enumerate(yw)]
        if err != MaxpoolError.NO_ERROR:
            halt(err, hdr.tx_id)
            break

    cur: list[int] = []
    for w, last in out:
        cur.append(w)
        if last:
            res.out.append(StreamBurst(np.array(cur, dtype=np.uint64), True))
            cur = []
    if cur:
        res.out.append(StreamBurst(np.array(cur, dtype=np.uint64), False))
    return res


# ---------------------------------------------------------------------------
# 3. The module: ports, register map, and the kernel body
# ---------------------------------------------------------------------------


@dataclass
class MaxpoolAccel(HostActivated):
    """The max-pool accelerator: a body-only kernel.

    The module declares the ports and the register map and names its body.  Waveflow
    generates ``gen/maxpool.hpp`` / ``gen/maxpool.cpp`` -- the prototype, every interface
    pragma, and one call ``maxpool_impl::body(s_in, m_out, halted, error, tx_id)`` -- and the
    whole kernel is the hand-written ``maxpool_body_impl.tpp``.  The register map holds only
    status; its fields reach the body by reference.  Everything the kernel computes with
    arrives on ``s_in`` (contract rule 2).
    """

    cpp_kernel_name: ClassVar[str | None] = "maxpool"
    # The hook-wrapping namespace must differ from the kernel function name, or
    # `::maxpool(...)` at the testbench call site is ambiguous.
    cpp_namespace:   ClassVar[str | None] = "maxpool_impl"
    cpp_body:        ClassVar[str | None] = "body"

    in_bw:        HwParam[int] = 32
    out_bw:       HwParam[int] = 32
    aximm_bw:     HwParam[int] = 32
    clk:          Clock = field(default_factory=lambda: Clock(freq=1e9))
    logger:       Logger | NullLogger = field(default_factory=NullLogger)

    def __post_init__(self) -> None:
        super().__post_init__()
        self.s_in  = StreamIFSlave( name=f'{self.name}_s_in',  sim=self.sim, bitwidth=self.in_bw)
        self.m_out = StreamIFMaster(name=f'{self.name}_m_out', sim=self.sim, bitwidth=self.out_bw)
        self.regmap = VitisRegMap({
            "halted": RegField(Bit,               RegAccess.R, description="1 = halted on error"),
            "error":  RegField(MaxpoolErrorField, RegAccess.R, description="Error code (MaxpoolError)"),
            "tx_id":  RegField(TxIdField,         RegAccess.R, description="tx_id of the failed command"),
        }, bitwidth=self.aximm_bw)
        self.s_lite = VitisRegMapMMIFSlave(
            name=f'{self.name}_s_lite', sim=self.sim, bitwidth=self.aximm_bw,
            regmap=self.regmap, on_start=self.on_start,
        )
        for ep in (self.s_in, self.m_out, self.s_lite):
            self.add_endpoint(ep)

    def _halt(self, err: MaxpoolError, tx_id: int) -> None:
        self.regmap.set("error", err)
        self.regmap.set("tx_id", tx_id)
        self.regmap.set("halted", 1)

    def body(self) -> ProcessGen[None]:
        """The kernel for pysim: the port wrapper around :func:`maxpool_eval`.

        Ordinary Python -- this is never translated to C++.  It is functional only: no
        latency is modelled.  A pysim stream reads a schema without exposing its TLAST, and
        cannot omit one, so header-framing and missing-TLAST scenarios do not run here.
        """
        self.logger.log(event='proc_begin', job=0)
        for f in ("halted", "error", "tx_id"):          # rule 5: this run's status only
            self.regmap.set(f, 0)
        while True:
            cmd_hdr: MaxpoolCmdHdr = yield from self.s_in.get_schema(MaxpoolCmdHdr)
            if cmd_hdr.cmd_type == MaxpoolCmdType.END:
                self.logger.log(event='proc_end', job=0)
                return
            if cmd_hdr.cmd_type != MaxpoolCmdType.COMPUTE:
                self._halt(MaxpoolError.UNKNOWN_CMD, cmd_hdr.tx_id)
                return
            n, k = int(cmd_hdr.n), int(cmd_hdr.k)
            if not params_ok(n, k):
                self._halt(MaxpoolError.BAD_PARAM, cmd_hdr.tx_id)
                return

            resp_hdr = MaxpoolRespHdr()
            resp_hdr.tx_id = cmd_hdr.tx_id
            yield from self.m_out.write(resp_hdr)

            # The burst ends at TLAST, so the words that arrived say whether it came early.
            pf = samples_per_word(self.in_bw)
            nwords_max = -(-n // pf)
            words = yield from self.s_in.get(nwords_max=nwords_max)
            x = read_array(words, elem_type=Int16, word_bw=self.in_bw,
                           shape=min(n, len(words) * pf)).val
            y = maxpool_eval(x, k)
            if len(y):
                yield from self.m_out.write(array(Int16, y))
            if len(words) < nwords_max:     # TLAST came early: halt, read nothing more
                self._halt(MaxpoolError.TLAST_EARLY_SAMP_IN, cmd_hdr.tx_id)
                return


# ---------------------------------------------------------------------------
# 4. The pysim testbench
# ---------------------------------------------------------------------------

#: What the status registers hold before a run: what a kernel that forgot to clear its
#: status (rule 5) would leave behind.  Both testbenches poison them, so that kernel fails.
POISONED_STATUS = {"halted": 1, "error": int(MaxpoolError.NO_TLAST_SAMP_IN), "tx_id": 0xFFFF}


@dataclass(kw_only=True)
class MaxpoolTB(SimObj):
    """Plays a scenario's stimulus file into :class:`MaxpoolAccel` and records the response.

    The stimulus is the same burst bundle the C++ testbench plays (``<scenario>/in``).  One
    kernel run: the testbench poisons the status registers, starts the kernel and sleeps
    until its ``ap_done`` interrupt, while two other processes push the stimulus and collect
    the response.  Then it reads the status.  After an error, whatever the kernel left unread
    stays in the input channel and the scenario ends: that is the host resetting the stream
    path (rule 7).
    """

    stimulus:  Path
    n_out:     int                       # output bursts to collect
    word_bw:   int = 32
    base_addr: int = 0x0

    def __post_init__(self) -> None:
        super().__post_init__()
        self.m_in   = StreamIFMaster(name=f'{self.name}_m_in',   sim=self.sim, bitwidth=self.word_bw)
        self.s_out  = StreamIFSlave( name=f'{self.name}_s_out',  sim=self.sim, bitwidth=self.word_bw)
        self.m_lite = MMIFMaster(    name=f'{self.name}_m_lite', sim=self.sim, bitwidth=32)
        self.irq    = IrqIFSink(     name=f'{self.name}_irq',    sim=self.sim)
        self.out: list[StreamBurst] = []
        self.status: dict[str, int] = {}
        self._regmap_ref: VitisRegMap | None = None

    def run_proc(self) -> ProcessGen[None]:
        for f, v in POISONED_STATUS.items():     # left over from "a previous run"
            self._regmap().set(f, v)
        rm = self._regmap().bind_master(self.m_lite, base_addr=self.base_addr)
        reader = self.env.process(self._read_all())
        self.env.process(self._write_all())
        yield from rm.run(self.irq)              # enable ap_done irq, start, sleep until done
        yield reader
        for f in ("halted", "error", "tx_id"):
            self.status[f] = int((yield from rm.get(f)))    # int: written as JSON

    def _write_all(self) -> ProcessGen[None]:
        for b in read_bursts(self.stimulus):
            yield from self.m_in.write(np.asarray(b.words, dtype=np.uint32))

    def _read_all(self) -> ProcessGen[None]:
        for _ in range(self.n_out):
            words = yield from self.s_out.get()
            self.out.append(StreamBurst(np.asarray(words, dtype=np.uint64), True))

    def _regmap(self) -> VitisRegMap:
        if self._regmap_ref is None:
            raise RuntimeError("MaxpoolTB._regmap_ref is unset; call connect() before run_sim().")
        return self._regmap_ref


def connect(sim: Simulation, tb: MaxpoolTB, accel: MaxpoolAccel, clk: Clock) -> None:
    """Wire the testbench to the accelerator: two streams, AXI-Lite, and the interrupt line."""
    in_stream  = StreamIF(sim=sim, clk=clk)
    out_stream = StreamIF(sim=sim, clk=clk)
    lite_link  = DirectMMIF(sim=sim, clk=clk, byte_addressable=True)
    irq        = IrqIF(name=f'{accel.name}_irq', sim=sim)
    in_stream.bind( "master", tb.m_in)
    in_stream.bind( "slave",  accel.s_in)
    out_stream.bind("master", accel.m_out)
    out_stream.bind("slave",  tb.s_out)
    lite_link.bind( "master", tb.m_lite)
    lite_link.bind( "slave",  accel.s_lite)
    irq.bind("source", accel.s_lite.interrupt())
    irq.bind("sink",   tb.irq)
    tb._regmap_ref = accel.regmap
