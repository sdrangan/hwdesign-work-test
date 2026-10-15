"""qam16_llr.py -- the 16-QAM max-log LLR demapper, written hook-first.

What is in this file, in the order a reader needs it:

1. **Schemas** -- the command header (which carries ``nsamp`` and ``inv_var``), the response
   header, the error codes, and the two array elements: the complex int16 sample and the int8
   LLR.  Waveflow generates their C++ headers and serializers; nothing packs words by hand.
2. **The bit-exact model** -- pure functions with no simulator in them:
   :func:`qam16_llr_eval` (the integer arithmetic, in the C++ operation order) and
   :func:`qam16_llr_stream_model` (the whole protocol over a stream of bursts, TLAST rules and
   parameter checks included).  This is what the C++ kernel is checked against, word for word.
3. **The module** -- :class:`Qam16LlrAccel` declares the ports and the register map (status
   only) and names its kernel body (``cpp_body = "body"``).  Waveflow generates the kernel's
   boundary (the prototype and every interface pragma) and a single call to the hand-written
   C++ body, ``qam16_llr_body_impl.tpp``.  The Python :meth:`Qam16LlrAccel.body` is the same
   kernel for pysim: a thin port wrapper around :func:`qam16_llr_eval`, plus a timing model.
4. **The pysim testbench** -- :class:`Qam16LlrTB` plays a scenario's stimulus file into the
   module, the same file the hand-written C++ testbench (``qam16_llr_tb.cpp``) plays into the
   Vitis kernel.

The scenarios, their independently computed expected outputs, and the checker are in
``scenarios.py``; the build that runs everything is ``qam16_llr_build.py``.
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
from waveflow.hw.complexfield import ComplexField
from waveflow.hw.dataschema import DataList, EnumField, IntField
from waveflow.hw.hw_hostactivated import HostActivated
from waveflow.hw.hw_module import HwParam
from waveflow.hw.interface import StreamIF, StreamIFMaster, StreamIFSlave
from waveflow.hw.irq import IrqIF, IrqIFSink
from waveflow.hw.timed_phase import TimedPhase
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
#: The stream word width (frame F2): one complex sample in, four LLRs out, per word.
WORD_BW_SUPPORTED = [32]
NSAMP_MIN = 1
NSAMP_MAX = 1024

CmdTypeField = IntField.specialize(bitwidth=8, signed=False)
TxIdField = IntField.specialize(bitwidth=16, signed=False)
NsampField = IntField.specialize(bitwidth=16, signed=False)
InvVarField = IntField.specialize(bitwidth=16, signed=False)    # raw u8.8
#: One sample component, raw s8.8; the sample is (re = yI, im = yQ).
Int16 = IntField.specialize(bitwidth=16, signed=True, include_dir=INCLUDE_DIR)
CInt16 = ComplexField.specialize(Int16)
#: One LLR, raw s6.2.
Int8 = IntField.specialize(bitwidth=8, signed=True, include_dir=INCLUDE_DIR)
LLRS_PER_SAMP = 4


class Qam16LlrError(IntEnum):
    """What the kernel reports in its ``error`` register when it halts."""
    NO_ERROR = 0
    TLAST_EARLY_SAMP_IN = 1   # TLAST arrived before the last sample word
    NO_TLAST_SAMP_IN = 2      # the last sample word had no TLAST
    BAD_PARAM = 3             # nsamp outside 1..1024
    UNKNOWN_CMD = 4           # cmd_type names no command

Qam16LlrErrorField = EnumField.specialize(enum_type=Qam16LlrError)


class Qam16LlrCmdType(IntEnum):
    """The command codes.  ``cmd_type`` is a plain 8-bit field, not an EnumField, so that a
    header with a code naming no command can be built through the schema (decisions.md item 9)
    and the kernel can report it as ``UNKNOWN_CMD``."""
    DEMAP = 0
    END = 1

Qam16LlrCmdTypeField = EnumField.specialize(enum_type=Qam16LlrCmdType)


class Qam16LlrCmdHdr(DataList):
    """Command header: everything one command needs, ``inv_var`` included.

    A ``DEMAP`` command is self-contained: it depends on nothing sent before it (contract
    rule 4), so the kernel keeps no configuration between commands.  ``END`` ends the
    activation; its ``nsamp`` and ``inv_var`` are sent as zeros and ignored.
    """
    elements = {
        "cmd_type": {"schema": CmdTypeField, "description": "Qam16LlrCmdType: DEMAP=0, END=1"},
        "tx_id":    {"schema": TxIdField,    "description": "Command ID: echoed, or reported on error"},
        "nsamp":    {"schema": NsampField,   "description": "Sample count, 1..1024 (0 for END)"},
        "inv_var":  {"schema": InvVarField,  "description": "Inverse noise variance, raw u8.8"},
    }


class Qam16LlrRespHdr(DataList):
    """Response header: echo of the DEMAP command's transaction ID."""
    elements = {
        "tx_id": {"schema": TxIdField, "description": "Echo of the transaction ID"},
    }


SCHEMA_CLASSES = [
    Qam16LlrErrorField,
    Qam16LlrCmdTypeField,
    Qam16LlrCmdHdr,
    Qam16LlrRespHdr,
]
#: The array elements whose packing helpers (``<elem>_array_utils.h``) the kernel uses.
ARRAY_ELEMS = [CInt16, Int8]

# ---------------------------------------------------------------------------
# 2. The bit-exact model (pure functions)
# ---------------------------------------------------------------------------


def _sat8(v: np.ndarray) -> np.ndarray:
    return np.clip(v, -128, 127)


def qam16_llr_eval(inv_var: int, yi: npt.ArrayLike, yq: npt.ArrayLike) -> npt.NDArray[np.int64]:
    """The four max-log LLRs of each sample, shape ``(n, 4)``, order b0, b1, b2, b3.

    All values are raw integers: ``yi``, ``yq`` s8.8 (int16), ``inv_var`` u8.8 (uint16), the
    LLRs s6.2 (int8).  In the C++ operation order and widths:

    - ``a = |y|`` (17 bits unsigned: ``|-32768| = 32768``), ``d = 512 - a`` (17 bits signed);
    - ``p = inv_var * y`` and ``q = inv_var * d``: exact products, 16 fractional bits;
    - ``LLR = sat8((p + 2**13) >> 14)``: an arithmetic shift, so round half up, then
      saturation to [-128, 127].
    """
    iv = np.int64(int(inv_var))
    out = []
    for y in (np.asarray(yi, dtype=np.int64), np.asarray(yq, dtype=np.int64)):
        d = np.int64(512) - np.abs(y)
        for p in (iv * y, iv * d):
            out.append(_sat8((p + np.int64(1 << 13)) >> np.int64(14)))
    return np.stack(out, axis=-1).astype(np.int64)


def samples_to_array(yi: npt.ArrayLike, yq: npt.ArrayLike) -> np.ndarray:
    """(yI, yQ) as the CInt16 element array that serializes to the sample burst."""
    yi, yq = np.asarray(yi), np.asarray(yq)
    x = np.zeros(len(yi), dtype=CInt16.init_value().dtype)
    x["re"], x["im"] = yi, yq
    return x


@dataclass
class Qam16LlrStreamResult:
    """What the kernel does to one stream: its output bursts and its final register status."""

    out: list[StreamBurst]
    halted: int = 0
    error: int = int(Qam16LlrError.NO_ERROR)
    tx_id: int = 0

    def status(self) -> dict[str, int]:
        return {"halted": self.halted, "error": self.error, "tx_id": self.tx_id}


def qam16_llr_stream_model(bursts: list[StreamBurst], word_bw: int = 32) -> Qam16LlrStreamResult:
    """One activation of the kernel, as a pure function of its input stream.

    Word by word, exactly what ``qam16_llr_body_impl.tpp`` does:

    - the status starts clear (rule 5);
    - read a command header (a fixed number of words, set by the schema; the header read does
      not inspect TLAST); on ``END``, return;
    - a ``cmd_type`` that is neither ``DEMAP`` nor ``END`` is ``UNKNOWN_CMD``; a ``DEMAP`` with
      ``nsamp`` outside 1..1024 is ``BAD_PARAM``.  Both halt before anything is written for the
      command and before any sample is read;
    - write the response header, TLAST on its last word;
    - read the sample burst a word (one complex sample) at a time, writing the word of its four
      LLRs, TLAST on the word that completes ``nsamp``;
    - a TLAST before the last sample word is ``TLAST_EARLY_SAMP_IN``: the output word just
      written gets TLAST too, closing the burst (rule 6).  No TLAST on the last sample word is
      ``NO_TLAST_SAMP_IN``.  Every error sets ``halted``, ``error`` and ``tx_id`` and returns at
      once: the rest of the input is left unread.

    The output is split into bursts at TLAST -- the only boundary on the wire -- so it
    compares directly with what ``wf::record_stream`` records from the C++ testbench.
    """
    if word_bw not in WORD_BW_SUPPORTED:
        raise ValueError(f"word_bw must be one of {WORD_BW_SUPPORTED}, got {word_bw}")
    words = [(int(w), i + 1 == len(b.words) and b.tlast)
             for b in bursts for i, w in enumerate(np.asarray(b.words, dtype=np.uint64))]
    hdr_words = Qam16LlrCmdHdr().serialize(word_bw=word_bw).size
    out: list[tuple[int, bool]] = []
    res = Qam16LlrStreamResult(out=[])
    pos = 0
    while True:
        if pos + hdr_words > len(words):
            raise ValueError("the stimulus ends before an END command: the kernel would block")
        hdr = Qam16LlrCmdHdr().deserialize([w for w, _ in words[pos:pos + hdr_words]], word_bw=word_bw)
        pos += hdr_words
        cmd, nsamp = int(hdr.cmd_type), int(hdr.nsamp)
        if cmd == Qam16LlrCmdType.END:
            break
        err = Qam16LlrError.NO_ERROR
        if cmd != Qam16LlrCmdType.DEMAP:
            err = Qam16LlrError.UNKNOWN_CMD
        elif not NSAMP_MIN <= nsamp <= NSAMP_MAX:
            err = Qam16LlrError.BAD_PARAM
        if err == Qam16LlrError.NO_ERROR:
            resp = Qam16LlrRespHdr()
            resp.tx_id = hdr.tx_id
            rw = resp.serialize(word_bw=word_bw)
            out += [(int(w), i + 1 == rw.size) for i, w in enumerate(rw)]
            for i in range(nsamp):
                if pos >= len(words):
                    raise ValueError("the stimulus ends inside a sample burst: the kernel would block")
                w, last = words[pos]
                pos += 1
                x = read_array([w], elem_type=CInt16, word_bw=word_bw, shape=1).val
                llr = qam16_llr_eval(int(hdr.inv_var), x["re"], x["im"]).reshape(-1)
                y = write_array(llr.astype(np.int8), elem_type=Int8, word_bw=word_bw)
                final = i + 1 == nsamp
                out.append((int(y[0]), final or last))
                if last and not final:
                    err = Qam16LlrError.TLAST_EARLY_SAMP_IN
                    break
                if final and not last:
                    err = Qam16LlrError.NO_TLAST_SAMP_IN
        if err != Qam16LlrError.NO_ERROR:
            res.halted, res.error, res.tx_id = 1, int(err), int(hdr.tx_id)  # int: status() -> json
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
class Qam16LlrAccel(HostActivated):
    """The 16-QAM LLR demapper: a body-only kernel.

    The module declares the ports and the register map and names its body.  Waveflow
    generates ``gen/qam16_llr.hpp`` / ``gen/qam16_llr.cpp`` -- the prototype, every interface
    pragma, and one call ``qam16_llr_impl::body(s_in, m_out, halted, error, tx_id)`` -- and the
    whole kernel is the hand-written ``qam16_llr_body_impl.tpp``.  The register map holds only
    status; its fields reach the body by reference, so it writes ``halted`` / ``error`` /
    ``tx_id`` itself.  Everything the kernel computes with arrives on ``s_in`` (contract rule 2).
    """

    cpp_kernel_name: ClassVar[str | None] = "qam16_llr"
    # The hook-wrapping namespace must differ from the kernel function name, or
    # `::qam16_llr(...)` at the testbench call site is ambiguous.
    cpp_namespace:   ClassVar[str | None] = "qam16_llr_impl"
    cpp_body:        ClassVar[str | None] = "body"

    in_bw:        HwParam[int] = 32
    out_bw:       HwParam[int] = 32
    aximm_bw:     HwParam[int] = 32
    clk:          Clock = field(default_factory=lambda: Clock(freq=1e9))
    # The timing model (pysim only): the samples' first result proc_latency cycles after their
    # first word, then one word per proc_ii cycles.  Calibrated against cosim: II = 1 from
    # csynth; with the seed proc_latency = 3, pysim gave 1031 cycles for rand_1024 against
    # cosim's 1039, so proc_latency = 3 + 8 = 11 (results.md).
    proc_ii:      int = 1
    proc_latency: int = 11
    logger:       Logger | NullLogger = field(default_factory=NullLogger)

    def __post_init__(self) -> None:
        super().__post_init__()
        self.s_in  = StreamIFSlave( name=f'{self.name}_s_in',  sim=self.sim, bitwidth=self.in_bw)
        self.m_out = StreamIFMaster(name=f'{self.name}_m_out', sim=self.sim, bitwidth=self.out_bw)
        self.regmap = VitisRegMap({
            "halted": RegField(Bit,                RegAccess.R, description="1 = halted on error"),
            "error":  RegField(Qam16LlrErrorField, RegAccess.R, description="Error code (Qam16LlrError)"),
            "tx_id":  RegField(TxIdField,          RegAccess.R, description="tx_id of the failed command"),
        }, bitwidth=self.aximm_bw)
        self.s_lite = VitisRegMapMMIFSlave(
            name=f'{self.name}_s_lite', sim=self.sim, bitwidth=self.aximm_bw,
            regmap=self.regmap, on_start=self.on_start,
        )
        for ep in (self.s_in, self.m_out, self.s_lite):
            self.add_endpoint(ep)
        self._job: int = 0
        # Two timed phases, as in stream_inband: the response header after the command header
        # (sequential), and the samples -> LLR loop (pipelined: a latency, then one word per
        # proc_ii cycles).  The numbers are the seeds; there is no calib/ directory.
        cal = Path(__file__).resolve().parent / "calib" / f"w{int(self.in_bw)}"
        self.hdr_phase = TimedPhase(
            "hdr", spans={"resp_time": (self.s_in.last, self.m_out.first)},
            seed={"resp_time": {"intercept": 0.0}}, calib_dir=cal / "hdr")
        self.samp_phase = TimedPhase(
            "samples", spans={"latency": (self.s_in.first, self.m_out.first),
                              "fill_time": (self.m_out.first, self.m_out.last)},
            features=("nwords",),
            seed={"latency": {"intercept": float(self.proc_latency), "nwords": 0.0},
                  "fill_time": {"intercept": 0.0, "nwords": float(self.proc_ii)}},
            calib_dir=cal / "samples")
        for ph in (self.hdr_phase, self.samp_phase):
            self.add_timing_phase(ph)

    def _halt(self, err: Qam16LlrError, tx_id: int) -> None:
        self.regmap.set("error", err)
        self.regmap.set("tx_id", tx_id)
        self.regmap.set("halted", 1)

    def _respond(self, wait, resp_hdr: "Qam16LlrRespHdr") -> ProcessGen[None]:
        """The response header: after the header phase's wait (*wait*, already taken), written."""
        yield wait
        yield from self.m_out.write(resp_hdr)

    def body(self) -> ProcessGen[None]:
        """The kernel for pysim: the port wrapper around :func:`qam16_llr_eval`, plus timing."""
        self.logger.log(event='proc_begin', job=self._job)
        for f in ("halted", "error", "tx_id"):          # rule 5: this run's status only
            self.regmap.set(f, 0)
        while True:
            cmd_hdr: Qam16LlrCmdHdr = yield from self.s_in.get_schema(Qam16LlrCmdHdr)
            cmd, nsamp = int(cmd_hdr.cmd_type), int(cmd_hdr.nsamp)
            if cmd == Qam16LlrCmdType.END:
                self.logger.log(event='proc_end', job=self._job)
                return
            if cmd != Qam16LlrCmdType.DEMAP or not NSAMP_MIN <= nsamp <= NSAMP_MAX:
                self._halt(Qam16LlrError.UNKNOWN_CMD if cmd != Qam16LlrCmdType.DEMAP
                           else Qam16LlrError.BAD_PARAM, cmd_hdr.tx_id)
                self.logger.log(event='proc_end', job=self._job)
                return

            resp_hdr = Qam16LlrRespHdr()
            resp_hdr.tx_id = cmd_hdr.tx_id
            resp = self.env.process(self._respond(self.hdr_phase.timeout(), resp_hdr))

            # A burst that ends early is shorter; its LLRs still go out as one burst, closed.
            load = self.s_in.get_array(CInt16, nsamp)
            store = self.m_out.write_array(Int8, LLRS_PER_SAMP * nsamp)
            inv_var = int(cmd_hdr.inv_var)

            def compute(x):
                llr = qam16_llr_eval(inv_var, x.val["re"], x.val["im"]).reshape(-1)
                return array(Int8, llr.astype(np.int8))

            r = yield from self.samp_phase.pipeline(compute, load=load, store=store,
                                                    nwords=load.nwords())
            yield resp
            self._job += 1

            if len(r.y.val) < LLRS_PER_SAMP * nsamp:   # TLAST came early: halt, read nothing more
                self._halt(Qam16LlrError.TLAST_EARLY_SAMP_IN, cmd_hdr.tx_id)
                self.logger.log(event='proc_end', job=self._job)
                return


# ---------------------------------------------------------------------------
# 4. The pysim testbench
# ---------------------------------------------------------------------------

#: What the status registers hold before a run: what a kernel that forgot to clear its
#: status (rule 5) would leave behind.  Both testbenches poison them, so that kernel fails.
POISONED_STATUS = {"halted": 1, "error": int(Qam16LlrError.NO_TLAST_SAMP_IN), "tx_id": 0xFFFF}


@dataclass(kw_only=True)
class Qam16LlrTB(SimObj):
    """Plays a scenario's stimulus file into :class:`Qam16LlrAccel` and records the response.

    One kernel run: the testbench poisons the status registers, starts the kernel and sleeps
    until its ``ap_done`` interrupt, while two other processes push the stimulus and collect
    the response.  Then it reads the status.  After an error, whatever the kernel left unread
    stays in the input channel and the scenario ends: that is the host resetting the stream
    path (rule 7).  A pysim stream cannot omit TLAST, so the missing-TLAST scenario runs only
    through the pure model and the C++ kernel.
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
            raise RuntimeError("Qam16LlrTB._regmap_ref is unset; call connect() before run_sim().")
        return self._regmap_ref


def connect(sim: Simulation, tb: Qam16LlrTB, accel: Qam16LlrAccel, clk: Clock) -> None:
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
