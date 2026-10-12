"""maxpool.py -- the streaming 2-D max-pool accelerator, written hook-first.

What is in this file, in the order a reader needs it:

1. **Schemas** -- the command header (which carries the image geometry), the response header
   and the error codes.  Waveflow generates their C++ headers and serializers; nothing packs
   words by hand.  Pixels travel as ``Int16`` arrays through the Waveflow array utilities.
2. **The bit-exact model** -- pure functions with no simulator in them:
   :func:`maxpool_eval` (the arithmetic: a zero-padded max pool on int16) and
   :func:`maxpool_stream_model` (the whole protocol over a stream of bursts, TLAST rules
   included).  This is what the C++ kernel is checked against, word for word.
3. **The module** -- :class:`MaxpoolAccel` declares the ports and the register map (status
   only) and names its kernel body (``cpp_body = "body"``).  Waveflow generates the
   kernel's boundary and a single call to the hand-written C++ body,
   ``maxpool_body_impl.tpp``.  The Python :meth:`MaxpoolAccel.body` is the same kernel for
   pysim: a thin port wrapper around :func:`maxpool_eval`, plus a timing model.
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
WORD_BW_SUPPORTED = [32]
TxIdField = IntField.specialize(bitwidth=16, signed=False)
DimField = IntField.specialize(bitwidth=16, signed=False)      # n0, n1: 0..65535 on the wire
SmallField = IntField.specialize(bitwidth=8, signed=False)     # s0, s1, k0, k1, cmd_type
Int16 = IntField.specialize(bitwidth=16, signed=True, include_dir=INCLUDE_DIR)

#: The limits of the spec.  Anything outside them is BAD_PARAM.
N_MAX = 256
KS_MAX = 8


class MaxpoolError(IntEnum):
    """What the kernel reports in its ``error`` register when it halts."""
    NO_ERROR = 0
    TLAST_EARLY_SAMP_IN = 1   # TLAST arrived before the last image word
    NO_TLAST_SAMP_IN = 2      # the last image word had no TLAST
    BAD_PARAM = 3             # n0, n1, s0, s1, k0 or k1 outside its limits
    BAD_CMD = 4               # cmd_type names no command
    TLAST_EARLY_HDR_IN = 5    # TLAST arrived before the last command-header word
    NO_TLAST_HDR_IN = 6       # the last command-header word had no TLAST

MaxpoolErrorField = EnumField.specialize(enum_type=MaxpoolError)


class MaxpoolCmdType(IntEnum):
    """The command codes.  On the wire ``cmd_type`` is a plain 8-bit field, so that a host
    (or a scenario) can send a value that names no command: that is ``BAD_CMD``."""
    COMPUTE = 0
    END = 1

#: Generated only for its C++ enum (the codes the body compares ``cmd_type`` against).
MaxpoolCmdTypeField = EnumField.specialize(enum_type=MaxpoolCmdType)


class MaxpoolCmdHdr(DataList):
    """Command header: everything one command needs, the geometry included.

    A ``COMPUTE`` command is self-contained: it depends on nothing sent before it (contract
    rule 4).  It is followed by the image, ``n0 * n1`` int16 pixels, row-major.  ``END``
    ends the activation; its other fields are sent as zeros and ignored.
    """
    elements = {
        "cmd_type": {"schema": SmallField, "description": "COMPUTE = 0, END = 1; others: BAD_CMD"},
        "tx_id":    {"schema": TxIdField,  "description": "Command ID: echoed, or reported on error"},
        "n0":       {"schema": DimField,   "description": "Image rows, 1..256"},
        "n1":       {"schema": DimField,   "description": "Image columns, 1..256"},
        "s0":       {"schema": SmallField, "description": "Row stride, 1..8"},
        "s1":       {"schema": SmallField, "description": "Column stride, 1..8"},
        "k0":       {"schema": SmallField, "description": "Window rows, 1..8"},
        "k1":       {"schema": SmallField, "description": "Window columns, 1..8"},
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


@dataclass(frozen=True)
class Geometry:
    """One COMPUTE command's parameters."""
    n0: int
    n1: int
    s0: int
    s1: int
    k0: int
    k1: int

    @property
    def m0(self) -> int:
        return self.n0 // self.s0

    @property
    def m1(self) -> int:
        return self.n1 // self.s1

    def valid(self) -> bool:
        return (1 <= self.n0 <= N_MAX and 1 <= self.n1 <= N_MAX
                and all(1 <= v <= KS_MAX for v in (self.s0, self.s1, self.k0, self.k1)))


def maxpool_eval(x: npt.ArrayLike, g: Geometry) -> npt.NDArray[np.int16]:
    """``Y[i, j] = max X[s0*i + a, s1*j + b]`` over ``0 <= a < k0``, ``0 <= b < k1``, with
    ``X`` zero outside the image, for ``0 <= i < n0 // s0`` and ``0 <= j < n1 // s1``.

    ``x`` is the image, ``(n0, n1)`` or flat row-major.  Max is exact on int16, so there is
    no rounding or saturation to pin down: the only choices are the zero padding and the
    floor in the output size, both written out here as directly as possible.
    """
    xs = np.asarray(x, dtype=np.int16).reshape(g.n0, g.n1)
    y = np.zeros((g.m0, g.m1), dtype=np.int16)
    for i in range(g.m0):
        for j in range(g.m1):
            best = None
            for a in range(g.k0):
                for b in range(g.k1):
                    r, c = g.s0 * i + a, g.s1 * j + b
                    v = int(xs[r, c]) if (r < g.n0 and c < g.n1) else 0
                    best = v if best is None else max(best, v)
            y[i, j] = best
    return y


def maxpool_rows(g: Geometry) -> int:
    """Rows the kernel's pixel loop walks: the image, then the zero rows below it that the
    last output row's window still covers (they are not read from the stream)."""
    return max(g.n0, g.s0 * (g.m0 - 1) + g.k0) if g.m0 > 0 else g.n0


def maxpool_emitted(g: Geometry, npix: int) -> int:
    """Outputs the kernel has sent after ``npix`` image pixels: ``Y[i, j]`` goes out at the
    pixel ``(s0*i + k0 - 1, min(s1*j + k1 - 1, n1 - 1))`` that completes its window (a window
    past the bottom edge completes only in the zero rows after the image)."""
    if g.m0 == 0 or g.m1 == 0:
        return 0
    last_row = g.s0 * np.arange(g.m0) + g.k0 - 1
    last_col = np.minimum(g.s1 * np.arange(g.m1) + g.k1 - 1, g.n1 - 1)
    return int(np.count_nonzero(last_row[:, None] * g.n1 + last_col[None, :] < npix))


@dataclass
class MaxpoolStreamResult:
    """What the kernel does to one stream: its output bursts and its final register status."""

    out: list[StreamBurst]
    halted: int = 0
    error: int = int(MaxpoolError.NO_ERROR)
    tx_id: int = 0

    def status(self) -> dict[str, int]:
        return {"halted": self.halted, "error": self.error, "tx_id": self.tx_id}


def maxpool_stream_model(bursts: list[StreamBurst], word_bw: int = 32) -> MaxpoolStreamResult:
    """One activation of the kernel, as a pure function of its input stream.

    Word by word, what ``maxpool_body_impl.tpp`` does:

    - the status starts clear (rule 5);
    - read the command header a word at a time, stopping at a TLAST: one before the last
      header word is ``TLAST_EARLY_HDR_IN``, none on it ``NO_TLAST_HDR_IN``;
    - ``END``: return.  Any other code but ``COMPUTE``: ``BAD_CMD``.  A parameter outside
      its limits: ``BAD_PARAM``.  Nothing is written for any of these;
    - write the response header, TLAST on its last word;
    - read the image a word at a time (two pixels each) up to its TLAST, and write the
      outputs completed so far (:func:`maxpool_emitted`; all of ``Y`` once the image is
      whole) as one burst closed with TLAST -- also when the TLAST came early (rule 6);
    - an early TLAST is ``TLAST_EARLY_SAMP_IN``; none on the last word ``NO_TLAST_SAMP_IN``.
      Every error sets ``halted``, ``error`` and ``tx_id`` and returns at once: the rest of
      the input is left unread.

    The output is split into bursts at TLAST -- the only boundary on the wire -- so it
    compares directly with what ``wf::record_stream`` records from the C++ testbench.
    """
    if word_bw not in WORD_BW_SUPPORTED:
        raise ValueError(f"word_bw must be one of {WORD_BW_SUPPORTED}, got {word_bw}")
    words = [(int(w), i + 1 == len(b.words) and b.tlast)
             for b in bursts for i, w in enumerate(np.asarray(b.words, dtype=np.uint64))]
    hdr_words = MaxpoolCmdHdr().serialize(word_bw=word_bw).size
    out: list[tuple[int, bool]] = []
    res = MaxpoolStreamResult(out=[])
    pos = 0

    def take() -> tuple[int, bool]:
        nonlocal pos
        if pos >= len(words):
            raise ValueError("the stimulus ends before an END command: the kernel would block")
        pos += 1
        return words[pos - 1]

    def halt(err: MaxpoolError, tx_id: int) -> None:
        res.halted, res.error, res.tx_id = 1, int(err), int(tx_id)  # int: status() -> json

    while True:
        hw: list[int] = []
        last = False
        while len(hw) < hdr_words and not last:
            w, last = take()
            hw.append(w)
        hdr = MaxpoolCmdHdr().deserialize(hw + [0] * (hdr_words - len(hw)), word_bw=word_bw)
        if len(hw) < hdr_words:
            halt(MaxpoolError.TLAST_EARLY_HDR_IN, hdr.tx_id)
            break
        if not last:
            halt(MaxpoolError.NO_TLAST_HDR_IN, hdr.tx_id)
            break
        if hdr.cmd_type == MaxpoolCmdType.END:
            break
        if hdr.cmd_type != MaxpoolCmdType.COMPUTE:
            halt(MaxpoolError.BAD_CMD, hdr.tx_id)
            break
        g = Geometry(int(hdr.n0), int(hdr.n1), int(hdr.s0), int(hdr.s1), int(hdr.k0), int(hdr.k1))
        if not g.valid():
            halt(MaxpoolError.BAD_PARAM, hdr.tx_id)
            break
        resp = MaxpoolRespHdr()
        resp.tx_id = hdr.tx_id
        rw = resp.serialize(word_bw=word_bw)
        out += [(int(w), i + 1 == rw.size) for i, w in enumerate(rw)]

        npix = g.n0 * g.n1
        nwords = -(-npix // 2)
        img: list[int] = []
        last = False
        while len(img) < nwords and not last:
            w, last = take()
            img.append(w)
        nrecv = min(2 * len(img), npix)
        x = np.zeros(npix, dtype=np.int16)
        x[:nrecv] = read_array(img, elem_type=Int16, word_bw=word_bw, shape=nrecv).val
        y = maxpool_eval(x, g).ravel()
        if nrecv < npix:
            y = y[:maxpool_emitted(g, nrecv)]
        yw = write_array(y, elem_type=Int16, word_bw=word_bw) if y.size else []
        out += [(int(w), i + 1 == len(yw)) for i, w in enumerate(yw)]
        if nrecv < npix:
            halt(MaxpoolError.TLAST_EARLY_SAMP_IN, hdr.tx_id)
            break
        if not last:
            halt(MaxpoolError.NO_TLAST_SAMP_IN, hdr.tx_id)
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
    # The timing model (pysim only), calibrated against RTL cosim: a COMPUTE takes one
    # pixel-loop iteration per pixel (and per zero-row pixel below the image) at proc_ii
    # cycles each, plus proc_latency cycles of header handling, pipeline fill and drain.
    proc_ii:      int = 1
    proc_latency: int = 40
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
        self._job: int = 0

    def _halt(self, err: MaxpoolError, tx_id: int) -> None:
        self.regmap.set("error", err)
        self.regmap.set("tx_id", tx_id)
        self.regmap.set("halted", 1)
        self.logger.log(event='proc_end', job=self._job)

    def body(self) -> ProcessGen[None]:
        """The kernel for pysim: the port wrapper around :func:`maxpool_eval`, plus timing.

        Ordinary Python -- this is never translated to C++.  It reads and writes the ports,
        calls the pure model for the arithmetic, and models time.  A pysim stream always
        closes a burst with TLAST, so the missing-TLAST errors cannot occur here.
        """
        self.logger.log(event='proc_begin', job=self._job)
        for f in ("halted", "error", "tx_id"):          # rule 5: this run's status only
            self.regmap.set(f, 0)
        hdr_words = MaxpoolCmdHdr().serialize(word_bw=self.in_bw).size
        period = self.clk.period
        while True:
            hw = yield from self.s_in.get(nwords_max=hdr_words)
            hw = [int(w) for w in hw]
            hdr = MaxpoolCmdHdr().deserialize(hw + [0] * (hdr_words - len(hw)),
                                              word_bw=self.in_bw)
            if len(hw) < hdr_words:
                self._halt(MaxpoolError.TLAST_EARLY_HDR_IN, hdr.tx_id)
                return
            if hdr.cmd_type == MaxpoolCmdType.END:
                self.logger.log(event='proc_end', job=self._job)
                return
            if hdr.cmd_type != MaxpoolCmdType.COMPUTE:
                self._halt(MaxpoolError.BAD_CMD, hdr.tx_id)
                return
            g = Geometry(int(hdr.n0), int(hdr.n1), int(hdr.s0), int(hdr.s1),
                         int(hdr.k0), int(hdr.k1))
            if not g.valid():
                self._halt(MaxpoolError.BAD_PARAM, hdr.tx_id)
                return

            resp_hdr = MaxpoolRespHdr()
            resp_hdr.tx_id = hdr.tx_id
            yield from self.m_out.write(resp_hdr)

            # The image ends at TLAST, so the words that arrived say whether it came early.
            npix = g.n0 * g.n1
            t_req = self.env.now
            words = yield from self.s_in.get(nwords_max=-(-npix // 2))
            # The pixel loop starts at the first word's arrival, or when the kernel asked for
            # it if the host had queued the image already.
            tstart = max(t_req, self.env.now - (len(words) - 1) * period)
            nrecv = min(2 * len(words), npix)
            x = np.zeros(npix, dtype=np.int16)
            x[:nrecv] = read_array(words, elem_type=Int16, word_bw=self.in_bw, shape=nrecv).val
            y = maxpool_eval(x, g).ravel()
            iters = maxpool_rows(g) * g.n1
            if nrecv < npix:
                y = y[:maxpool_emitted(g, nrecv)]
                iters = nrecv
            t_done = tstart + (iters * self.proc_ii + self.proc_latency) * period
            yield self.timeout(max(0.0, t_done - self.env.now))
            if y.size:
                nw_out = -(-y.size // 2)
                yield from self.m_out.write_pipelined(array(Int16, y),
                                                      self.env.now - nw_out * period)
            self._job += 1
            if nrecv < npix:                    # TLAST came early: halt, read nothing more
                self._halt(MaxpoolError.TLAST_EARLY_SAMP_IN, hdr.tx_id)
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
    the response.  Then it reads the status.  After an error, whatever the kernel left
    unread stays in the input channel and the scenario ends: that is the host resetting the
    stream path (rule 7).
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
