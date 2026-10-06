"""fir.py -- the command-driven streaming FIR accelerator, written hook-first.

What is in this file, in the order a reader needs it:

1. **Schemas** -- the command header, response header, response footer, status message,
   error codes and the int16 sample/tap element.  Waveflow generates their C++ headers and
   serializers; nothing packs words by hand.
2. **The bit-exact model** -- pure functions with no simulator in them:
   :func:`fir_eval` (the arithmetic: exact accumulate, round-half-up, saturate),
   :func:`fir_command` (one command, word by word, TLAST rules and errors included) and
   :func:`fir_stream_model` (every command of an input stream).  The kernel state that
   persists between commands (taps, delay line, counters) is :class:`FirState`.
3. **The module** -- :class:`FirAccel` declares the two streams and names its kernel body
   (``cpp_body = "body"``).  Waveflow generates the kernel's boundary and a single call to the
   hand-written C++ body, ``fir_body_impl.tpp``.  ``param_supports`` gives a second top,
   ``fir_bw64``, for 64-bit words.  The Python :meth:`FirAccel.body` is the same kernel for
   pysim: a port wrapper around :func:`fir_command`, plus a timing model.
4. **The pysim testbench** -- :class:`FirTB` plays a scenario's stimulus file into the
   module, the same file the hand-written C++ testbench (``fir_tb.cpp``) plays into Vitis.

The scenarios, their independently computed expected outputs, and the checker are in
``scenarios.py``; the build that runs everything is ``fir_build.py``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import ClassVar

import numpy as np
import numpy.typing as npt

from waveflow.hw.arrayutils import read_array, write_array
from waveflow.hw.clock import Clock
from waveflow.hw.dataschema import DataList, EnumField, IntField
from waveflow.hw.hw_hostactivated import HostActivated
from waveflow.hw.hw_module import HwParam
from waveflow.hw.interface import StreamIF, StreamIFMaster, StreamIFSlave
from waveflow.hw.memif import DirectMMIF, MMIFMaster
from waveflow.hw.regmap import VitisRegMap, VitisRegMapMMIFSlave
from waveflow.simulation.logger import Logger, NullLogger
from waveflow.simulation.simobj import ProcessGen, SimObj
from waveflow.simulation.simulation import Simulation
from waveflow.utils.burst_io import StreamBurst, read_bursts

# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# 2. The bit-exact model (pure functions)
# ---------------------------------------------------------------------------


def fir_round_sat(acc: npt.ArrayLike) -> npt.NDArray[np.int64]:
    """``sat16((acc + 2^14) >> 15)``: round half up, arithmetic shift, clip to int16."""
    a = np.asarray(acc, dtype=np.int64)
    return np.clip((a + (1 << 14)) >> 15, -32768, 32767).astype(np.int64)


def fir_eval(taps: npt.ArrayLike, x: npt.ArrayLike,
             hist: npt.ArrayLike | None = None) -> tuple[npt.NDArray[np.int64], npt.NDArray[np.int64]]:
    """Filter ``x`` with ``taps``, continuing from the delay line ``hist``.

    ``acc[n] = sum_k h[k] x[n-k]`` exactly (int64; the C++ uses a 40-bit accumulator, and
    32 products of two int16 need at most 37 bits), then ``y = fir_round_sat(acc)``.

    ``hist`` is the previous ``NTAPS_MAX - 1`` input samples, oldest first (zeros if None).
    Returns ``(y, new_hist)``: one output per input sample (decimation is the caller's), and
    the delay line after the last sample of ``x``.
    """
    h = np.asarray(taps, dtype=np.int64)
    if not 1 <= h.size <= NTAPS_MAX:
        raise ValueError(f"1..{NTAPS_MAX} taps, got {h.size}")
    hl = (np.zeros(NTAPS_MAX - 1, dtype=np.int64) if hist is None
          else np.asarray(hist, dtype=np.int64))
    xs = np.asarray(x, dtype=np.int64)
    full = np.concatenate([hl, xs])
    L = hl.size
    acc = np.zeros(xs.size, dtype=np.int64)
    for k in range(h.size):
        acc += h[k] * full[L - k:L - k + xs.size]
    return fir_round_sat(acc), full[-(NTAPS_MAX - 1):].copy()


@dataclass
class FirState:
    """Everything the kernel keeps between commands, at power-up."""
    taps: npt.NDArray[np.int64] = field(default_factory=lambda: np.zeros(NTAPS_MAX, np.int64))
    ntaps: int = 0
    hist: npt.NDArray[np.int64] = field(default_factory=lambda: np.zeros(NTAPS_MAX - 1, np.int64))
    nsamp_total: int = 0
    nerr: int = 0
    last_error: int = int(FirError.NO_ERROR)

    def copy(self) -> "FirState":
        return FirState(self.taps.copy(), self.ntaps, self.hist.copy(), self.nsamp_total,
                        self.nerr, self.last_error)


class WordReader:
    """The input stream as the kernel sees it: one ``(word, tlast)`` at a time."""

    def __init__(self, words: list[tuple[int, bool]], pos: int = 0) -> None:
        self.words, self.pos = words, pos

    def more(self) -> bool:
        return self.pos < len(self.words)

    def read(self) -> tuple[int, bool]:
        if self.pos >= len(self.words):
            raise ValueError("the stimulus ends where the kernel still reads: it would block")
        w = self.words[self.pos]
        self.pos += 1
        return w


def _lanes(word: int, word_bw: int) -> npt.NDArray[np.int64]:
    """The ``word_bw / 16`` int16 lanes of one word, through the Waveflow array utilities."""
    pf = word_bw // 16
    dt = np.uint32 if word_bw <= 32 else np.uint64
    return np.asarray(read_array(np.array([word], dtype=dt), elem_type=Int16,
                                 word_bw=word_bw, shape=pf).val, dtype=np.int64)


def words_of(burst: npt.NDArray, tlast: bool = True) -> list[tuple[int, bool]]:
    """A burst's words, each paired with its TLAST flag."""
    ws = [int(w) for w in np.asarray(burst, dtype=np.uint64)]
    return [(w, tlast and i + 1 == len(ws)) for i, w in enumerate(ws)]


def fir_command(state: FirState, hdr: FirCmdHdr, rd: WordReader,
                word_bw: int) -> tuple[list[tuple[int, bool]], FirState]:
    """One command, after its header: what the kernel reads, writes and remembers.

    Exactly what ``fir_body_impl.tpp`` does for one call:

    - write the response header;
    - check the header (``BAD_OPCODE``; ``BAD_NTAPS``; ``NO_TAPS`` then ``BAD_DECIM``).  On a
      header error with ``count > 0`` the payload burst is discarded through its TLAST;
    - otherwise read ``ceil(count / pf)`` payload words, stopping at a TLAST
      (``TLAST_EARLY`` if that is before the last word); if the last word has no TLAST, the
      input is discarded through the next TLAST (``NO_TLAST``);
    - ``PROCESS`` writes the kept outputs (``y[n]``, ``n % D == 0``), ``pf`` per word, TLAST
      on the last word; ``STATUS`` writes the status message;
    - on any error ``nerr`` / ``last_error`` are updated and the delay line is zeroed;
    - write the footer.

    Returns the output words with their TLAST flags, and the state after the command.
    """
    pf = word_bw // 16
    st = state.copy()
    out: list[tuple[int, bool]] = []

    def emit(words: npt.NDArray) -> None:
        out.extend(words_of(words))

    def discard() -> None:
        while not rd.read()[1]:
            pass

    resp = FirRespHdr()
    resp.tx_id, resp.opcode = int(hdr.tx_id), int(hdr.opcode)
    emit(resp.serialize(word_bw=word_bw))

    op, cnt = int(hdr.opcode), int(hdr.count)
    decim, reset = int(hdr.decim), int(hdr.reset)
    err, nin, nout = FirError.NO_ERROR, 0, 0

    def read_payload(on_lanes) -> FirError:
        """Read the payload words, calling ``on_lanes(lanes, n0, c, word_is_final)``."""
        nonlocal nin
        nwords = (cnt + pf - 1) // pf
        for w in range(nwords):
            word, last = rd.read()
            c = min(pf, cnt - w * pf)
            final = last or w + 1 == nwords
            on_lanes(_lanes(word, word_bw)[:c], w * pf, final)
            nin += c
            if last:
                return FirError.TLAST_EARLY if w + 1 < nwords else FirError.NO_ERROR
        discard()
        return FirError.NO_TLAST

    if op == FirOpcode.LOAD_TAPS:
        if cnt == 0 or cnt > NTAPS_MAX:
            err = FirError.BAD_NTAPS
            if cnt:
                discard()
        else:
            shadow: list[int] = []
            err = read_payload(lambda lanes, n0, final: shadow.extend(int(v) for v in lanes))
            if err == FirError.NO_ERROR:
                st.taps = np.zeros(NTAPS_MAX, np.int64)
                st.taps[:cnt] = shadow
                st.ntaps = cnt
                st.nsamp_total = 0
                st.hist = np.zeros(NTAPS_MAX - 1, np.int64)
    elif op == FirOpcode.PROCESS:
        if st.ntaps == 0:
            err = FirError.NO_TAPS
        elif decim not in (1, 2, 4) or cnt % decim:
            err = FirError.BAD_DECIM
        if err != FirError.NO_ERROR:
            if cnt:
                discard()
        else:
            if reset:
                st.hist = np.zeros(NTAPS_MAX - 1, np.int64)
            kept: list[int] = []

            def on_lanes(lanes, n0, final):
                y, st.hist = fir_eval(st.taps[:st.ntaps], lanes, st.hist)
                kept.extend(int(y[k]) for k in range(lanes.size) if (n0 + k) % decim == 0)

            if cnt:
                err = read_payload(on_lanes)
                st.nsamp_total = (st.nsamp_total + nin) & 0xFFFFFFFF
                # The y burst: every kept output, pf per word, TLAST on its last word -- also
                # when the payload ended early or without TLAST.
                nout = len(kept)
                if kept:
                    out.extend(words_of(write_array(np.array(kept, dtype=np.int16),
                                                    elem_type=Int16, word_bw=word_bw)))
    elif op == FirOpcode.STATUS:
        msg = FirStatusMsg()
        msg.ntaps, msg.nsamp_total = st.ntaps, st.nsamp_total
        msg.nerr, msg.last_error = st.nerr, st.last_error
        emit(msg.serialize(word_bw=word_bw))
    else:
        err = FirError.BAD_OPCODE

    if err != FirError.NO_ERROR:
        st.nerr = min(st.nerr + 1, NERR_MAX)
        st.last_error = int(err)
        st.hist = np.zeros(NTAPS_MAX - 1, np.int64)
    ftr = FirRespFtr()
    ftr.nin, ftr.nout, ftr.error = nin, nout, err
    emit(ftr.serialize(word_bw=word_bw))
    return out, st


def split_at_tlast(words: list[tuple[int, bool]]) -> list[StreamBurst]:
    """Split ``(word, tlast)`` pairs into bursts at TLAST -- the only boundary on the wire."""
    res: list[StreamBurst] = []
    cur: list[int] = []
    for w, last in words:
        cur.append(w)
        if last:
            res.append(StreamBurst(np.array(cur, dtype=np.uint64), True))
            cur = []
    if cur:
        res.append(StreamBurst(np.array(cur, dtype=np.uint64), False))
    return res


def fir_stream_model(bursts: list[StreamBurst], word_bw: int = 32,
                     state: FirState | None = None) -> tuple[list[StreamBurst], FirState]:
    """The whole kernel, as a pure function of its input stream and its starting state.

    Reads a command header (its word count is fixed by the schema; the header read does not
    inspect TLAST) and runs :func:`fir_command`, until the stream is used up.  Returns the
    output split at TLAST, so it compares directly with what ``wf::record_stream`` records,
    and the state left behind.
    """
    words: list[tuple[int, bool]] = []
    for b in bursts:
        words += words_of(b.words, b.tlast)
    rd = WordReader(words)
    st = FirState() if state is None else state.copy()
    hdr_words = FirCmdHdr().serialize(word_bw=word_bw).size
    dt = np.uint32 if word_bw <= 32 else np.uint64
    out: list[tuple[int, bool]] = []
    while rd.more():
        hw = np.array([rd.read()[0] for _ in range(hdr_words)], dtype=dt)
        hdr = FirCmdHdr().deserialize(hw, word_bw=word_bw)
        o, st = fir_command(st, hdr, rd, word_bw)
        out += o
    return split_at_tlast(out), st


# ---------------------------------------------------------------------------
# 3. The module: ports and the kernel body
# ---------------------------------------------------------------------------


@dataclass
class FirAccel(HostActivated):
    """The FIR accelerator: a body-only kernel with two AXI4-Streams and no register fields.

    Waveflow generates ``gen/fir.hpp`` / ``gen/fir.cpp`` -- the prototype, every interface
    pragma, and one call ``fir_impl::body(s_in, m_out)`` -- for ``fir`` (32-bit words) and
    ``fir_bw64`` (64-bit words).  The whole kernel is the hand-written ``fir_body_impl.tpp``:
    one command per call, its state in ``static`` storage that persists between calls.
    """

    cpp_kernel_name: ClassVar[str | None] = "fir"
    cpp_namespace:   ClassVar[str | None] = "fir_impl"
    cpp_body:        ClassVar[str | None] = "body"
    param_supports:  ClassVar[dict] = {"bw64": {"in_bw": 64, "out_bw": 64}}

    in_bw:        HwParam[int] = 32
    out_bw:       HwParam[int] = 32
    aximm_bw:     HwParam[int] = 32
    clk:          Clock = field(default_factory=lambda: Clock(freq=1e9))
    # The timing model (pysim only), calibrated from the cosim VCD (results.md): a payload
    # of N words is read one word per proc_ii cycles; the y burst ends proc_latency cycles
    # after N * proc_ii, and the footer follows.  Cosim measured, from the first payload word
    # to the last footer word, N + 20 cycles at 32 bits (2-word footer) and N + 21 at 64 bits
    # (1-word footer): proc_latency = 19 is within 2 cycles of both.
    proc_ii:      int = 1
    proc_latency: int = 19
    logger:       Logger | NullLogger = field(default_factory=NullLogger)
    init_state:   FirState = field(default_factory=FirState)

    def __post_init__(self) -> None:
        super().__post_init__()
        self.s_in  = StreamIFSlave( name=f'{self.name}_s_in',  sim=self.sim, bitwidth=self.in_bw)
        self.m_out = StreamIFMaster(name=f'{self.name}_m_out', sim=self.sim, bitwidth=self.out_bw)
        self.regmap = VitisRegMap({}, bitwidth=self.aximm_bw)
        self.s_lite = VitisRegMapMMIFSlave(
            name=f'{self.name}_s_lite', sim=self.sim, bitwidth=self.aximm_bw,
            regmap=self.regmap, on_start=self.on_start,
        )
        for ep in (self.s_in, self.m_out, self.s_lite):
            self.add_endpoint(ep)
        self.state = self.init_state.copy()
        self._job: int = 0
        #: Per command with a payload: (job, opcode, count, first payload word time, footer end).
        self.timing: list[tuple[int, int, int, float, float]] = []

    def body(self) -> ProcessGen[None]:
        """The kernel for pysim: the port wrapper around :func:`fir_command`, plus timing.

        Ordinary Python -- never translated to C++.  pysim delivers whole bursts, so it runs
        the scenarios whose every burst ends in TLAST (``scenarios.WELL_FORMED``).
        """
        word_bw = self.in_bw
        period = self.clk.period
        while True:
            hdr: FirCmdHdr = yield from self.s_in.get_schema(FirCmdHdr)
            self.logger.log(event='cmd_begin', job=self._job)
            op, cnt = int(hdr.opcode), int(hdr.count)
            payload: list[tuple[int, bool]] = []
            t_first = self.env.now
            if op in (FirOpcode.LOAD_TAPS, FirOpcode.PROCESS) and cnt > 0:
                raw = yield from self.s_in.get()
                payload = words_of(np.asarray(raw, dtype=np.uint64))
                t_first = self.env.now - (len(payload) - 1) * period
                self.logger.log(event='samp_read_begin', job=self._job)
            out, self.state = fir_command(self.state, hdr, WordReader(payload), word_bw)
            bursts = split_at_tlast(out)
            yield from self.m_out.write(np.asarray(bursts[0].words))          # RespHdr
            if len(bursts) > 2:                                                 # y or StatusMsg
                body = np.asarray(bursts[1].words)
                t_out = t_first + (len(payload) * self.proc_ii + self.proc_latency
                                   - body.size) * period
                yield from self.m_out.write_pipelined(body, t_out)
            yield from self.m_out.write(np.asarray(bursts[-1].words))         # RespFtr
            self.logger.log(event='ftr_write_end', job=self._job)
            if payload:
                self.timing.append((self._job, op, cnt, t_first, self.env.now))
            self._job += 1


# ---------------------------------------------------------------------------
# 4. The pysim testbench
# ---------------------------------------------------------------------------


@dataclass(kw_only=True)
class FirTB(SimObj):
    """Plays a scenario's stimulus file into :class:`FirAccel` and records the response.

    The stimulus is the same burst bundle the C++ testbench plays (``<scenario>/in``).  The
    testbench asserts ``ap_start`` and then pushes and pops in two concurrent processes, as
    hardware would.
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
        self.out: list[StreamBurst] = []
        self._regmap_ref: VitisRegMap | None = None

    def run_proc(self) -> ProcessGen[None]:
        rm = self._regmap().bind_master(self.m_lite, base_addr=self.base_addr)
        yield from rm.start()
        reader = self.env.process(self._read_all())
        word_t = np.uint32 if self.word_bw <= 32 else np.uint64
        for b in read_bursts(self.stimulus):
            yield from self.m_in.write(np.asarray(b.words, dtype=word_t))
        yield reader

    def _read_all(self) -> ProcessGen[None]:
        for _ in range(self.n_out):
            words = yield from self.s_out.get()
            self.out.append(StreamBurst(np.asarray(words, dtype=np.uint64), True))

    def _regmap(self) -> VitisRegMap:
        if self._regmap_ref is None:
            raise RuntimeError("FirTB._regmap_ref is unset; call connect() before run_sim().")
        return self._regmap_ref


def connect(sim: Simulation, tb: FirTB, accel: FirAccel, clk: Clock) -> None:
    """Wire the testbench to the accelerator: two streams and the AXI-Lite (ap_start) link."""
    in_stream  = StreamIF(sim=sim, clk=clk)
    out_stream = StreamIF(sim=sim, clk=clk)
    lite_link  = DirectMMIF(sim=sim, clk=clk, byte_addressable=True)
    in_stream.bind( "master", tb.m_in)
    in_stream.bind( "slave",  accel.s_in)
    out_stream.bind("master", accel.m_out)
    out_stream.bind("slave",  tb.s_out)
    lite_link.bind( "master", tb.m_lite)
    lite_link.bind( "slave",  accel.s_lite)
    tb._regmap_ref = accel.regmap
