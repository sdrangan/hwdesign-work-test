"""maxpool.py -- the streaming 1-D max-pool accelerator, written hook-first.

What is in this file, in the order a reader needs it:

1. **Schemas** -- the command header (which carries ``tx_id``, ``n`` and ``k``), the response
   header, the command codes and the error codes.  Waveflow generates their C++ headers and
   serializers; nothing packs words by hand.  Samples and results are int16, two to a 32-bit
   word, packed by the generated ``int16_array_utils``.
2. **The bit-exact model** -- pure functions with no simulator in them:
   :func:`maxpool_eval` (the arithmetic: non-overlapping windows of ``k`` samples, the max of
   each, computed as a running max in the order the C++ uses) and :func:`maxpool_stream_model`
   (the whole protocol over a stream of bursts, header and signal TLAST rules included).
3. **The module** -- :class:`MaxpoolAccel` declares the ports and the register map (status
   only) and names its kernel body (``cpp_body = "body"``).  Waveflow generates the kernel's
   boundary and a single call to the hand-written C++ body, ``maxpool_body_impl.tpp``.  The
   Python :meth:`MaxpoolAccel.body` is the same kernel for pysim: a thin port wrapper around
   :func:`maxpool_eval`, plus a timing model.
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
WORD_BW = 32
WORD_BW_SUPPORTED = [WORD_BW]
TxIdField = IntField.specialize(bitwidth=16, signed=False)
LenField = IntField.specialize(bitwidth=16, signed=False)
#: The raw command code on the wire.  An IntField, not the EnumField, so that a header can
#: carry -- and a scenario can serialize -- a code that names no command (UNKNOWN_CMD).
CmdCodeField = IntField.specialize(bitwidth=8, signed=False)
Int16 = IntField.specialize(bitwidth=16, signed=True, include_dir=INCLUDE_DIR)

#: Limits from the spec: 1 <= n <= N_MAX, 1 <= k <= min(n, K_MAX).
N_MAX = 128
K_MAX = 8


class MaxpoolError(IntEnum):
    """What the kernel reports in its ``error`` register when it halts."""
    NO_ERROR = 0
    TLAST_EARLY_SAMP_IN = 1   # signal burst: TLAST before the last sample word
    NO_TLAST_SAMP_IN = 2      # signal burst: the last sample word had no TLAST
    BAD_PARAM = 3             # COMPUTE header: n or k outside the limits
    TLAST_EARLY_HDR = 4       # header: TLAST before its last word
    NO_TLAST_HDR = 5          # header: its last word had no TLAST
    UNKNOWN_CMD = 6           # header: cmd_type names no command

MaxpoolErrorField = EnumField.specialize(enum_type=MaxpoolError)


class MaxpoolCmdType(IntEnum):
    COMPUTE = 0
    END = 1

#: Generated as a C++ ``enum class`` so the kernel compares the raw code against names.
MaxpoolCmdTypeField = EnumField.specialize(enum_type=MaxpoolCmdType, bitwidth=8)


class MaxpoolCmdHdr(DataList):
    """Command header.  ``COMPUTE`` is followed by the signal burst of ``n`` int16 samples;
    ``END`` ends the activation, with ``n``, ``k`` and ``tx_id`` sent as zeros and ignored.

    ``tx_id`` is declared last, so it lands in the header's second word (see layout.md): a
    header cut short by an early TLAST on its first word has not delivered its ``tx_id``.
    """
    elements = {
        "cmd_type": {"schema": CmdCodeField, "description": "MaxpoolCmdType code: COMPUTE or END"},
        "n":        {"schema": LenField,     "description": "Signal length in samples (0 for END)"},
        "k":        {"schema": LenField,     "description": "Window size in samples (0 for END)"},
        "tx_id":    {"schema": TxIdField,    "description": "Command ID: echoed, or reported on error"},
    }


class MaxpoolRespHdr(DataList):
    """Response header: echo of the COMPUTE command's transaction ID."""
    elements = {
        "tx_id": {"schema": TxIdField, "description": "Echo of the transaction ID"},
    }


SCHEMA_CLASSES = [
    MaxpoolErrorField,
    MaxpoolCmdTypeField,
    MaxpoolCmdHdr,
    MaxpoolRespHdr,
]

# ---------------------------------------------------------------------------
# 2. The bit-exact model (pure functions)
# ---------------------------------------------------------------------------


def params_ok(n: int, k: int) -> bool:
    """The parameter check: 1 <= n <= 128 and 1 <= k <= min(n, 8)."""
    return 1 <= n <= N_MAX and 1 <= k <= min(n, K_MAX)


def maxpool_eval(x: npt.ArrayLike, k: int) -> npt.NDArray[np.int16]:
    """Non-overlapping max pool with window ``k`` over the samples ``x``.

    ``y[i] = max(x[k*i : k*i + k])`` for every *whole* window; samples past the last whole
    window are ignored.  Computed the way the C++ does it -- a running max reset at the start
    of each window, a result emitted when the window fills -- so a partial signal (an early
    TLAST) gives exactly the windows the kernel completes.  No arithmetic beyond compare, so
    no rounding or saturation.
    """
    xs = np.asarray(x, dtype=np.int16)
    y: list[int] = []
    acc, a = 0, 0
    for v in xs:
        acc = int(v) if a == 0 else max(acc, int(v))
        a += 1
        if a == k:
            y.append(acc)
            a = 0
    return np.asarray(y, dtype=np.int16)


@dataclass
class MaxpoolStreamResult:
    """What the kernel does to one stream: its output bursts and its final register status."""

    out: list[StreamBurst]
    halted: int = 0
    error: int = int(MaxpoolError.NO_ERROR)
    tx_id: int = 0

    def status(self) -> dict[str, int]:
        return {"halted": self.halted, "error": self.error, "tx_id": self.tx_id}


def samples_per_word(word_bw: int = WORD_BW) -> int:
    """int16 samples in one stream word: 2 at 32 bits."""
    return word_bw // 16


def maxpool_stream_model(bursts: list[StreamBurst], word_bw: int = WORD_BW) -> MaxpoolStreamResult:
    """One activation of the kernel, as a pure function of its input stream.

    Word by word, what ``maxpool_body_impl.tpp`` does:

    - the status starts clear;
    - read a command header word by word.  TLAST on a word before its last is
      ``TLAST_EARLY_HDR`` (stop reading at once; ``tx_id`` is reported only if it had
      arrived, else 0); no TLAST on its last word is ``NO_TLAST_HDR``.  Then an unknown
      ``cmd_type`` is ``UNKNOWN_CMD``; on ``END``, return; then ``n`` / ``k`` outside the
      limits is ``BAD_PARAM`` -- all before anything is written for the command;
    - write the response header, TLAST on its last word;
    - read the signal a word (two samples) at a time, emitting the max of each whole window;
      results go out two to a word, the burst closed with TLAST on its last word;
    - a TLAST before the last signal word is ``TLAST_EARLY_SAMP_IN``: the windows completed so
      far are sent and the burst closed (no burst if none completed).  No TLAST on the last
      signal word is ``NO_TLAST_SAMP_IN``, after all of ``y`` is sent.  Every error sets
      ``halted``, ``error`` and ``tx_id`` and returns at once, leaving the rest unread.

    The output is split into bursts at TLAST -- the only boundary on the wire.
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

    def halt(err: MaxpoolError, tx_id: int) -> None:
        res.halted, res.error, res.tx_id = 1, int(err), int(tx_id)

    while True:
        # The header, word by word: stop at an early TLAST.
        got: list[int] = []
        hdr_err = MaxpoolError.NO_ERROR
        for j in range(hdr_words):
            if pos >= len(words):
                raise ValueError("the stimulus ends inside a header: the kernel would block")
            w, last = words[pos]
            pos += 1
            got.append(w)
            if last and j + 1 < hdr_words:
                hdr_err = MaxpoolError.TLAST_EARLY_HDR
                break
            if j + 1 == hdr_words and not last:
                hdr_err = MaxpoolError.NO_TLAST_HDR
        # Fields not delivered read as zero: the kernel zeroes the header before reading it.
        hdr = MaxpoolCmdHdr().deserialize(got + [0] * (hdr_words - len(got)), word_bw=word_bw)
        if hdr_err != MaxpoolError.NO_ERROR:
            halt(hdr_err, hdr.tx_id)
            break
        if hdr.cmd_type not in (MaxpoolCmdType.COMPUTE, MaxpoolCmdType.END):
            halt(MaxpoolError.UNKNOWN_CMD, hdr.tx_id)
            break
        if hdr.cmd_type == MaxpoolCmdType.END:
            break
        n, k = int(hdr.n), int(hdr.k)
        if not params_ok(n, k):
            halt(MaxpoolError.BAD_PARAM, hdr.tx_id)
            break

        resp = MaxpoolRespHdr()
        resp.tx_id = hdr.tx_id
        rw = resp.serialize(word_bw=word_bw)
        out += [(int(w), i + 1 == rw.size) for i, w in enumerate(rw)]

        nwords = -(-n // pf)
        x: list[int] = []
        err = MaxpoolError.NO_ERROR
        for i in range(nwords):
            if pos >= len(words):
                raise ValueError("the stimulus ends inside a signal burst: the kernel would block")
            w, last = words[pos]
            pos += 1
            nlane = min(pf, n - i * pf)
            x += [int(v) for v in read_array([w], elem_type=Int16, word_bw=word_bw, shape=nlane).val]
            final = i + 1 == nwords
            if last and not final:
                err = MaxpoolError.TLAST_EARLY_SAMP_IN
                break
            if final and not last:
                err = MaxpoolError.NO_TLAST_SAMP_IN
        y = maxpool_eval(x, k)
        if y.size:
            yw = write_array(y, elem_type=Int16, word_bw=word_bw)
            out += [(int(v), i + 1 == yw.size) for i, v in enumerate(yw)]
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
    status.  Everything the kernel computes with arrives on ``s_in``.
    """

    cpp_kernel_name: ClassVar[str | None] = "maxpool"
    # The hook-wrapping namespace must differ from the kernel function name, or
    # `::maxpool(...)` at the testbench call site is ambiguous.
    cpp_namespace:   ClassVar[str | None] = "maxpool_impl"
    cpp_body:        ClassVar[str | None] = "body"

    in_bw:        HwParam[int] = WORD_BW
    out_bw:       HwParam[int] = WORD_BW
    aximm_bw:     HwParam[int] = 32
    clk:          Clock = field(default_factory=lambda: Clock(freq=1e9))
    # The timing model (pysim only), calibrated against RTL cosim: one kernel call takes
    # the stream transfers (one word per cycle) plus proc_latency cycles of pipeline fill
    # and handshake overhead per COMPUTE command.
    proc_ii:      int = 1
    proc_latency: int = 40
    logger:       Logger | NullLogger = field(default_factory=NullLogger)
    unroll_factor: int = 1

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
        self._job: int = 0

    def _halt(self, err: MaxpoolError, tx_id: int) -> None:
        self.regmap.set("error", err)
        self.regmap.set("tx_id", tx_id)
        self.regmap.set("halted", 1)

    def body(self) -> ProcessGen[None]:
        """The kernel for pysim: the port wrapper around :func:`maxpool_eval`, plus timing.

        Ordinary Python -- never translated to C++.  pysim runs only the well-formed
        scenarios (a pysim stream cannot express a header or signal with a wrong TLAST), so
        the command and parameter checks are here, the TLAST checks on the signal only for
        the early case, as in the reference.
        """
        self.logger.log(event='proc_begin', job=self._job)
        for f in ("halted", "error", "tx_id"):          # this run's status only
            self.regmap.set(f, 0)
        while True:
            cmd_hdr: MaxpoolCmdHdr = yield from self.s_in.get_schema(MaxpoolCmdHdr)
            if cmd_hdr.cmd_type not in (MaxpoolCmdType.COMPUTE, MaxpoolCmdType.END):
                self._halt(MaxpoolError.UNKNOWN_CMD, cmd_hdr.tx_id)
                self.logger.log(event='proc_end', job=self._job)
                return
            if cmd_hdr.cmd_type == MaxpoolCmdType.END:
                self.logger.log(event='proc_end', job=self._job)
                return
            n, k = int(cmd_hdr.n), int(cmd_hdr.k)
            if not params_ok(n, k):
                self._halt(MaxpoolError.BAD_PARAM, cmd_hdr.tx_id)
                self.logger.log(event='proc_end', job=self._job)
                return

            resp_hdr = MaxpoolRespHdr()
            resp_hdr.tx_id = cmd_hdr.tx_id
            yield from self.m_out.write(resp_hdr)

            pf = samples_per_word(self.in_bw)
            nwords_max = -(-n // pf)
            words = yield from self.s_in.get(nwords_max=nwords_max)
            nwords = len(words)
            tstart = self.env.now - (nwords - 1) * self.clk.period    # first word's arrival
            x = read_array(words, elem_type=Int16, word_bw=self.in_bw,
                           shape=min(n, nwords * pf)).val
            y = maxpool_eval(x, k)

            t_out_start = tstart + self.proc_latency * self.clk.period
            proc_time = nwords / self.unroll_factor * self.proc_ii * self.clk.period
            proc_time = max(0.0, proc_time + (t_out_start - self.env.now))
            yield self.timeout(proc_time)
            if y.size:
                yield from self.m_out.write_pipelined(array(Int16, y), t_out_start)
            self._job += 1

            if nwords < nwords_max:              # TLAST came early: halt, read nothing more
                self._halt(MaxpoolError.TLAST_EARLY_SAMP_IN, cmd_hdr.tx_id)
                self.logger.log(event='proc_end', job=self._job)
                return


# ---------------------------------------------------------------------------
# 4. The pysim testbench
# ---------------------------------------------------------------------------

#: What the status registers hold before a run: what a kernel that forgot to clear its
#: status would leave behind.  Both testbenches poison them, so that kernel fails.
POISONED_STATUS = {"halted": 1, "error": int(MaxpoolError.NO_TLAST_SAMP_IN), "tx_id": 0xFFFF}


@dataclass(kw_only=True)
class MaxpoolTB(SimObj):
    """Plays a scenario's stimulus file into :class:`MaxpoolAccel` and records the response.

    One kernel run: the testbench poisons the status registers, starts the kernel and sleeps
    until its ``ap_done`` interrupt, while two other processes push the stimulus and collect
    the response.  Then it reads the status.
    """

    stimulus:  Path
    n_out:     int                       # output bursts to collect
    word_bw:   int = WORD_BW
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
