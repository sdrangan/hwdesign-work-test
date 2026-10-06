"""scenarios.py -- the test scenarios, their expected results, and the checker.

Each scenario is a named list of **intents**: "load these taps", "filter these samples with
D = 2", "a PROCESS whose payload stops 3 words in", "STATUS".  From the intents, this module
writes, for each word width W in {32, 64}:

- the stimulus, ``data/w<W>/<scenario>/in`` -- a burst bundle that the Python model, the
  pysim testbench and the C++ testbench all read -- and ``ncmd.txt``, the number of commands
  (the C++ testbench calls the kernel once per command);
- the expected response, ``data/w<W>/<scenario>/expected``.

The scenarios run **in order, as one session from power-up**: the kernel keeps its taps,
delay line and counters from one scenario to the next, exactly as it keeps them from one
command to the next, and nothing but power-up resets ``nerr``.  The expected response is
computed from the intents and an intended state (taps, the samples since the last delay-line
reset, the counters), never by parsing the stimulus, so it is an independent reference.
The arithmetic is :func:`fir.fir_eval`, pinned down by ``worked_examples.py``.

:func:`check` compares any run -- the pure model, pysim, csim, cosim -- against the expected
results, word for word, burst for burst and TLAST for TLAST, and also checks the
block-splitting property directly (``split_whole`` and ``split_parts`` produce one signal).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

try:
    from fir import (
        NERR_MAX, NTAPS_MAX, FirCmdHdr, FirError, FirOpcode, FirRespFtr, FirRespHdr, FirState,
        FirStatusMsg, Int16, fir_eval,
    )
except ModuleNotFoundError:  # imported from outside the project directory
    from .fir import (  # type: ignore[no-redef]
        NERR_MAX, NTAPS_MAX, FirCmdHdr, FirError, FirOpcode, FirRespFtr, FirRespHdr, FirState,
        FirStatusMsg, Int16, fir_eval,
    )
from waveflow.hw.arrayutils import read_array, write_array
from waveflow.utils.burst_io import StreamBurst, read_bursts, write_bursts

WORD_BWS = (32, 64)

# ---------------------------------------------------------------------------
# Intents
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Load:
    """LOAD_TAPS.  ``ntaps`` is what the header declares (default ``len(taps)``).  The host
    sends ``sent_words`` payload words (default all), TLAST on the last one if ``tlast``,
    then, if ``junk`` > 0, a burst of that many junk words ending in TLAST."""
    tx: int
    taps: tuple
    ntaps: int | None = None
    sent_words: int | None = None
    tlast: bool = True
    junk: int = 0

    @property
    def count(self) -> int:
        return len(self.taps) if self.ntaps is None else self.ntaps


@dataclass(frozen=True)
class Process:
    """PROCESS of the samples ``x`` (``nsamp = len(x)``), decimation ``decim``."""
    tx: int
    x: tuple
    decim: int = 1
    reset: int = 0
    sent_words: int | None = None
    tlast: bool = True
    junk: int = 0

    @property
    def count(self) -> int:
        return len(self.x)


@dataclass(frozen=True)
class Status:
    tx: int


@dataclass(frozen=True)
class Raw:
    """A header with an arbitrary opcode and nothing after it."""
    tx: int
    opcode: int
    count: int = 0


# ---------------------------------------------------------------------------
# Stimulus
# ---------------------------------------------------------------------------


def _vals(v, bw: int) -> np.ndarray:
    return write_array(np.asarray(v, dtype=np.int16), elem_type=Int16, word_bw=bw)


def _hdr(tx: int, opcode: int, count: int = 0, decim: int = 0, reset: int = 0, *, bw: int):
    h = FirCmdHdr()
    h.tx_id, h.opcode, h.count, h.decim, h.reset = tx, opcode, count, decim, reset
    return h.serialize(word_bw=bw)


def _payload(vals, sent_words, tlast, junk, bw) -> list[StreamBurst]:
    if len(vals) == 0:
        return []
    words = _vals(vals, bw)
    n = len(words) if sent_words is None else sent_words
    out = [StreamBurst(words[:n], tlast)]
    if junk:
        out.append(StreamBurst(_vals(np.full(junk * (bw // 16), 0x5A5A), bw), True))
    return out


def stimulus(intents: list, bw: int) -> list[StreamBurst]:
    bursts: list[StreamBurst] = []
    for c in intents:
        if isinstance(c, Load):
            bursts.append(StreamBurst(_hdr(c.tx, FirOpcode.LOAD_TAPS, c.count, bw=bw)))
            bursts += _payload(c.taps, c.sent_words, c.tlast, c.junk, bw)
        elif isinstance(c, Process):
            bursts.append(StreamBurst(_hdr(c.tx, FirOpcode.PROCESS, c.count, c.decim, c.reset, bw=bw)))
            bursts += _payload(c.x, c.sent_words, c.tlast, c.junk, bw)
        elif isinstance(c, Status):
            bursts.append(StreamBurst(_hdr(c.tx, FirOpcode.STATUS, bw=bw)))
        elif isinstance(c, Raw):
            bursts.append(StreamBurst(_hdr(c.tx, c.opcode, c.count, bw=bw)))
    return bursts


# ---------------------------------------------------------------------------
# Expected responses, from the intents
# ---------------------------------------------------------------------------


@dataclass
class Intended:
    """What the kernel should remember, in intent terms."""
    taps: tuple = ()
    since_reset: list = field(default_factory=list)   # samples since the delay line was zeroed
    nsamp_total: int = 0
    nerr: int = 0
    last_error: int = 0

    def hist(self) -> np.ndarray:
        h = np.zeros(NTAPS_MAX - 1, np.int64)
        tail = self.since_reset[-(NTAPS_MAX - 1):]
        if tail:
            h[-len(tail):] = tail
        return h

    def as_state(self) -> FirState:
        st = FirState()
        st.taps[:len(self.taps)] = self.taps
        st.ntaps, st.hist = len(self.taps), self.hist()
        st.nsamp_total, st.nerr, st.last_error = self.nsamp_total, self.nerr, self.last_error
        return st


def _framing(c, bw: int) -> tuple[FirError, int]:
    """For a command whose header is fine: the framing error and the values consumed."""
    pf = bw // 16
    nwords = -(-c.count // pf)
    sent = nwords if c.sent_words is None else c.sent_words
    if c.tlast and sent < nwords:
        return FirError.TLAST_EARLY, sent * pf
    if not c.tlast:
        return FirError.NO_TLAST, c.count
    return FirError.NO_ERROR, c.count


def expected(intents: list, bw: int, s: Intended) -> list[StreamBurst]:
    """The response to ``intents``, starting from (and updating) the intended state ``s``."""
    out: list[StreamBurst] = []
    for c in intents:
        opcode = (FirOpcode.LOAD_TAPS if isinstance(c, Load) else FirOpcode.PROCESS
                  if isinstance(c, Process) else FirOpcode.STATUS if isinstance(c, Status)
                  else c.opcode)
        rh = FirRespHdr()
        rh.tx_id, rh.opcode = c.tx, opcode
        out.append(StreamBurst(rh.serialize(word_bw=bw)))
        err, nin, nout = FirError.NO_ERROR, 0, 0
        if isinstance(c, Load):
            if not 1 <= c.count <= NTAPS_MAX:
                err = FirError.BAD_NTAPS
            else:
                err, nin = _framing(c, bw)
                if err == FirError.NO_ERROR:
                    s.taps, s.since_reset, s.nsamp_total = tuple(c.taps), [], 0
        elif isinstance(c, Process):
            if not s.taps:
                err = FirError.NO_TAPS
            elif c.decim not in (1, 2, 4) or c.count % c.decim:
                err = FirError.BAD_DECIM
            else:
                if c.reset:
                    s.since_reset = []
                err, nin = _framing(c, bw)
                if nin:
                    y, _ = fir_eval(s.taps, c.x[:nin], s.hist())
                    kept = y[::c.decim]
                    nout = len(kept)
                    out.append(StreamBurst(_vals(kept, bw)))
                s.since_reset = s.since_reset + list(c.x[:nin])
                s.nsamp_total += nin
        elif isinstance(c, Status):
            m = FirStatusMsg()
            m.ntaps, m.nsamp_total = len(s.taps), s.nsamp_total
            m.nerr, m.last_error = s.nerr, s.last_error
            out.append(StreamBurst(m.serialize(word_bw=bw)))
        else:
            err = FirError.BAD_OPCODE
        if err != FirError.NO_ERROR:
            s.nerr = min(s.nerr + 1, NERR_MAX)
            s.last_error = int(err)
            s.since_reset = []
        f = FirRespFtr()
        f.nin, f.nout, f.error = nin, nout, err
        out.append(StreamBurst(f.serialize(word_bw=bw)))
    return out


# ---------------------------------------------------------------------------
# The scenarios
# ---------------------------------------------------------------------------


def scenarios() -> dict[str, list]:
    """Every scenario, from fixed seeds, in session order."""
    rng = np.random.default_rng(2026)

    def x(n: int, amp: int = 32768) -> tuple:
        return tuple(int(v) for v in rng.integers(-amp, amp, n))

    def h(n: int, amp: int = 12000) -> tuple:
        return tuple(int(v) for v in rng.integers(-amp, amp, n))

    h13 = h(13)
    long_sig = x(300)
    cuts = [1, 7, 64, 3, 100, 125]                    # odd lengths: partial last words
    parts, i = [], 0
    for n in cuts:
        parts.append(long_sig[i:i + n])
        i += n
    assert i == len(long_sig)

    return {
        # Power-up: nothing loaded, no errors.  Then NO_TAPS, and STATUS after it.
        "status_powerup": [Status(1), Process(2, x(10)), Status(3)],
        # The cosim timing scenario: ntaps = 32, D = 1, two lengths.
        "timing": [Load(10, h(32)), Process(11, x(64)), Process(12, x(1024))],
        # Load, then a single PROCESS, at each decimation factor.
        "decim_1": [Load(20, h(8)), Process(21, x(40), decim=1)],
        "decim_2": [Load(22, h(8)), Process(23, x(40), decim=2)],
        "decim_4": [Load(24, h(8)), Process(25, x(40), decim=4)],
        # Block splitting: one 300-sample signal, whole and in six uneven pieces.
        "split_whole": [Load(30, h13), Process(31, long_sig)],
        "split_parts": [Load(32, h13)] + [Process(33 + k, p) for k, p in enumerate(parts)],
        # reset = 1 mid-stream; then a taps reload with a different ntaps, mid-stream.
        "reset_mid": [Load(40, h(6)), Process(41, x(50)), Process(42, x(50), reset=1),
                      Process(43, x(31))],
        "reload_mid": [Load(44, h(5)), Process(45, x(40)), Load(46, h(20)), Process(47, x(41)),
                       Status(48)],
        # STATUS after processing.
        "status_after": [Process(50, x(33)), Process(51, x(12), decim=4), Status(52)],
        # Every error code, each followed by a valid command whose result must be right.
        "err_bad_opcode": [Process(60, x(9)), Raw(61, 0), Process(62, x(9)), Raw(63, 7),
                           Raw(64, 255), Status(65), Process(66, x(5))],
        "err_bad_ntaps": [Load(70, (), ntaps=0), Process(71, x(11)),
                          Load(72, h(33)), Process(73, x(11)), Status(74)],
        "err_bad_decim": [Process(80, x(9), decim=3), Process(81, x(10), decim=4),
                          Process(82, x(8), decim=0), Process(83, x(12), decim=4), Status(84)],
        "err_tlast_early": [Process(90, x(17)), Process(91, x(40), decim=2, sent_words=5),
                            Process(92, x(17)), Load(93, h(20), sent_words=3),
                            Process(94, x(17)), Status(95)],
        "err_no_tlast": [Process(100, x(21), tlast=False, junk=3), Process(101, x(21)),
                         Load(102, h(6), tlast=False, junk=2), Process(103, x(9)), Status(104)],
        # NO_TAPS after power-up is in status_powerup.
        # ntaps = 1 and 32; nsamp = D, the shortest valid commands; nsamp >= 1000.
        "ntaps_1": [Load(110, (23170,)), Process(111, x(20))],
        "ntaps_32_short": [Load(112, h(32)), Process(113, x(1), decim=1),
                           Process(114, x(2), decim=2), Process(115, x(4), decim=4)],
        "long": [Process(116, x(1001)), Process(117, x(1000), decim=4)],
        # Full-scale taps and samples: saturation both ways.
        "saturate": [Load(120, (32767,) * 16 + (-32768,) * 16),
                     Process(121, (32767,) * 40 + (-32768,) * 40 + x(17))],
        # nsamp = 0: a valid PROCESS with no payload and no y burst.
        "zero_len": [Process(130, ()), Status(131)],
    }


#: Scenarios with no missing TLAST: the ones a pysim stream can express.
WELL_FORMED = tuple(n for n in scenarios() if n != "err_no_tlast")


def write_scenarios(data_root: Path) -> list[str]:
    """Write every scenario's stimulus and expected response, for both widths."""
    sc = scenarios()
    for bw in WORD_BWS:
        d0 = data_root / f"w{bw}"
        d0.mkdir(parents=True, exist_ok=True)
        s = Intended()
        for name, intents in sc.items():
            d = d0 / name
            d.mkdir(parents=True, exist_ok=True)
            st = s.as_state()
            (d / "state_in.json").write_text(json.dumps({
                "taps": [int(v) for v in st.taps], "ntaps": st.ntaps,
                "hist": [int(v) for v in st.hist], "nsamp_total": st.nsamp_total,
                "nerr": st.nerr, "last_error": st.last_error}) + "\n", encoding="utf-8")
            write_bursts(stimulus(intents, bw), d / "in")
            write_bursts(expected(intents, bw, s), d / "expected")
            (d / "ncmd.txt").write_text(f"{len(intents)}\n", encoding="utf-8")
        (d0 / "scenarios.txt").write_text("\n".join(sc) + "\n", encoding="utf-8")
    return list(sc)


def load_state(path: Path) -> FirState:
    j = json.loads(path.read_text(encoding="utf-8"))
    st = FirState()
    st.taps = np.array(j["taps"], np.int64)
    st.hist = np.array(j["hist"], np.int64)
    st.ntaps, st.nsamp_total, st.nerr, st.last_error = (
        j["ntaps"], j["nsamp_total"], j["nerr"], j["last_error"])
    return st


# ---------------------------------------------------------------------------
# The checker
# ---------------------------------------------------------------------------


def _compare(want: list[StreamBurst], got: list[StreamBurst]) -> list[str]:
    problems = []
    if len(want) != len(got):
        problems.append(f"{len(got)} bursts, expected {len(want)}")
    for k, (w, g) in enumerate(zip(want, got)):
        if w.tlast != g.tlast:
            problems.append(f"burst {k}: tlast={g.tlast}, expected {w.tlast}")
        if not np.array_equal(np.asarray(w.words, np.uint64), np.asarray(g.words, np.uint64)):
            n = min(len(w.words), len(g.words))
            bad = next((i for i in range(n) if int(w.words[i]) != int(g.words[i])), n)
            problems.append(f"burst {k}: {len(g.words)} words, expected {len(w.words)}; "
                            f"first difference at word {bad}")
    return problems


def _y_signal(bursts: list[StreamBurst], bw: int) -> list[int]:
    """The y samples of a scenario whose commands are one LOAD then PROCESSes (D = 1):
    responses are RespHdr | RespFtr for the LOAD, RespHdr | y | RespFtr per PROCESS.  The
    footer's nout says how many of the last word's lanes are real."""
    ys: list[int] = []
    k = 2
    dt = np.uint32 if bw <= 32 else np.uint64
    while k + 2 < len(bursts):
        ywords, ftr = bursts[k + 1].words, bursts[k + 2].words
        f = FirRespFtr().deserialize(np.asarray(ftr, dtype=dt), word_bw=bw)
        n = int(f.nout)
        vals = read_array(np.asarray(ywords, dtype=dt), elem_type=Int16, word_bw=bw,
                          shape=len(ywords) * (bw // 16)).val
        ys += [int(v) for v in np.asarray(vals)[:n]]
        k += 3
    return ys


def check_split(data_dir: Path, stage: str, bw: int) -> list[str]:
    """Block splitting: the pieces must give the same y as the whole, sample for sample."""
    try:
        whole = _y_signal(read_bursts(data_dir / "split_whole" / stage), bw)
        parts = _y_signal(read_bursts(data_dir / "split_parts" / stage), bw)
    except Exception as exc:  # a malformed recording is a failure, not a crash
        return [f"cannot decode: {exc}"]
    if len(whole) != 300 or whole != parts:
        n = min(len(whole), len(parts))
        bad = next((i for i in range(n) if whole[i] != parts[i]), n)
        return [f"whole {len(whole)} samples, parts {len(parts)}; first difference at {bad}"]
    return []


def check(data_dir: Path, stage: str, bw: int, names: list[str] | None = None) -> dict[str, list[str]]:
    """Compare ``<scenario>/<stage>`` against ``<scenario>/expected`` for each scenario.

    Returns ``{scenario: [problems]}``; an empty list is a pass.  Words, burst boundaries
    and TLAST flags must all match exactly.  ``split_property`` is the block-splitting check.
    """
    names = names or (data_dir / "scenarios.txt").read_text(encoding="utf-8").split()
    report: dict[str, list[str]] = {}
    for name in names:
        got_dir = data_dir / name / stage
        if not (got_dir / "words.bin").exists():
            report[name] = [f"no {stage} output in {got_dir}"]
            continue
        report[name] = _compare(read_bursts(data_dir / name / "expected"), read_bursts(got_dir))
    if {"split_whole", "split_parts"} <= set(names):
        report["split_property"] = check_split(data_dir, stage, bw)
    return report
