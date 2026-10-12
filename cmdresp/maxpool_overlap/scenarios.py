"""scenarios.py -- the test scenarios, their expected results, and the checker.

Each scenario is one kernel run (one ``ap_start``), written as a short list of **intents**:
"a COMPUTE command with this image and this geometry", "one whose image burst ends early",
"one whose header has no TLAST", "then END".  From the intents, this module writes

- the stimulus, ``<data>/w32/<scenario>/in`` -- a burst bundle that the Python model, the
  pysim testbench and the C++ testbench all read; and
- the expected response, ``<data>/w32/<scenario>/expected`` plus ``expected_status.json``.

The expected response is computed from the intent, not by parsing the stimulus, so it is
an independent reference: a model or a kernel that misreads the protocol cannot agree with
it by sharing the mistake.  (The arithmetic is :func:`maxpool.maxpool_eval`, which
``test_maxpool_eval.py`` pins down with hand-computed examples and a NumPy reference.)

The scenarios run in the order of :func:`scenarios`: every error activation is followed by a
fresh activation that must pass.  The error activations keep sending commands after the bad
one, as a host with commands in flight would.  The kernel must leave them unread (contract
rule 6); the host's reset, which discards them (rule 7), is the end of the activation.

:func:`check` compares any run -- the pure model, pysim, csim, cosim -- against the expected
results, word for word and TLAST for TLAST.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from maxpool import (
    WORD_BW_SUPPORTED, Geometry, Int16, MaxpoolCmdHdr, MaxpoolCmdType, MaxpoolError,
    MaxpoolRespHdr, maxpool_eval,
)
from waveflow.hw.arrayutils import write_array
from waveflow.utils.burst_io import StreamBurst, read_bursts, write_bursts

WORD_BW = 32
#: int16 pixels per 32-bit stream word (element 0 in the low half).
PIX_PER_WORD = 2


@dataclass(frozen=True)
class Cmd:
    """One command as intended.

    ``cmd_type`` is COMPUTE, END, or any other 8-bit value (an unknown command).  A COMPUTE
    (or an unknown command, or one with bad parameters) is followed by its image burst,
    ``x`` row-major -- unless the header itself is malformed and ``img`` is False.

    Framing faults:

    - ``hdr_words``: how many header words the host sends (default: all).  Fewer, with
      ``hdr_tlast``, is an early TLAST on the header.  ``hdr_tlast=False`` is a missing one.
    - ``img_words``: how many image words the host sends (default: all).  Fewer, with
      ``img_tlast``, is an early TLAST on the image.  ``img_tlast=False`` is a missing one.
    """

    tx_id: int
    g: Geometry
    x: np.ndarray | None = None
    cmd_type: int = int(MaxpoolCmdType.COMPUTE)
    hdr_words: int | None = None
    hdr_tlast: bool = True
    img: bool = True
    img_words: int | None = None
    img_tlast: bool = True

    @property
    def npix(self) -> int:
        return self.g.n0 * self.g.n1

    @property
    def nwords_img(self) -> int:
        return -(-self.npix // PIX_PER_WORD)


@dataclass(frozen=True)
class Scenario:
    """One kernel run: its commands, and whether a well-formed END follows them."""

    cmds: list[Cmd]
    end: bool = True          # False: nothing after the last command (it must be an error)
    timing: bool = False      # a timing scenario: its cosim cycle count is reported

    @property
    def pysim(self) -> bool:
        """A pysim stream always closes a burst with TLAST, so it cannot omit one."""
        return all(c.hdr_tlast and c.img_tlast for c in self.cmds)


def hdr_words_full() -> int:
    return int(MaxpoolCmdHdr().serialize(word_bw=WORD_BW).size)


def _hdr(c: Cmd | None) -> np.ndarray:
    h = MaxpoolCmdHdr()
    if c is None:                                # END
        h.cmd_type, h.tx_id = int(MaxpoolCmdType.END), 0
        h.n0 = h.n1 = h.s0 = h.s1 = h.k0 = h.k1 = 0
    else:
        h.cmd_type, h.tx_id = c.cmd_type, c.tx_id
        h.n0, h.n1, h.s0, h.s1, h.k0, h.k1 = c.g.n0, c.g.n1, c.g.s0, c.g.s1, c.g.k0, c.g.k1
    return h.serialize(word_bw=WORD_BW)


def _pixels(x: np.ndarray) -> np.ndarray:
    return write_array(np.asarray(x, dtype=np.int16).ravel(), elem_type=Int16, word_bw=WORD_BW)


def stimulus(sc: Scenario) -> list[StreamBurst]:
    """The input stream: each command's header and image, then END."""
    bursts: list[StreamBurst] = []
    for c in sc.cmds:
        hw = _hdr(c)
        nh = hw.size if c.hdr_words is None else c.hdr_words
        bursts.append(StreamBurst(hw[:nh], c.hdr_tlast))
        if c.img and c.npix > 0:
            pw = _pixels(c.x)
            nw = pw.size if c.img_words is None else c.img_words
            bursts.append(StreamBurst(pw[:nw], c.img_tlast))
    if sc.end:
        bursts.append(StreamBurst(_hdr(None)))
    return bursts


def n_emitted(g: Geometry, npix_received: int) -> int:
    """How many outputs a COMPUTE whose image ends early (TLAST after ``npix_received``
    pixels) has sent, in row-major order.

    Output ``Y[i, j]`` is sent once the last pixel its window needs has arrived: pixel
    ``(s0*i + k0 - 1, min(s1*j + k1 - 1, n1 - 1))``.  A window that runs past the bottom of
    the image needs rows that never arrive -- its zero padding is only known once the image
    is complete -- so such a row is never sent on an early TLAST.  The windows that run past
    the right edge are complete at the end of their row.
    """
    count = 0
    for i in range(g.m0):
        last_row = g.s0 * i + g.k0 - 1
        for j in range(g.m1):
            last_col = min(g.s1 * j + g.k1 - 1, g.n1 - 1)
            if last_row * g.n1 + last_col < npix_received:
                count += 1
    return count


def expected(sc: Scenario) -> tuple[list[StreamBurst], dict]:
    """The response the contract promises, from the intent alone.

    For each command, in order, the first fault that applies:

    1. a header with an early TLAST (``TLAST_EARLY_HDR_IN``) or none (``NO_TLAST_HDR_IN``):
       nothing is written for it;
    2. ``END``: the run ends cleanly;
    3. a ``cmd_type`` that names no command (``BAD_CMD``): nothing is written;
    4. a parameter outside its limits (``BAD_PARAM``): nothing is written;
    5. otherwise the response header, then ``Y`` as one burst closed with TLAST (no burst
       when ``Y`` is empty).  An early TLAST on the image sends only the outputs already
       complete (:func:`n_emitted`), closes that burst if it sent any, and halts with
       ``TLAST_EARLY_SAMP_IN``; a missing TLAST sends all of ``Y`` and halts with
       ``NO_TLAST_SAMP_IN``.

    A halt reports the failing command's ``tx_id`` (read from header word 0, which every
    header fault still delivers).  Nothing after a halt is answered.  The status starts clear.
    """
    out: list[StreamBurst] = []
    status = {"halted": 0, "error": int(MaxpoolError.NO_ERROR), "tx_id": 0}

    def halt(err: MaxpoolError, c: Cmd) -> None:
        status.update(halted=1, error=int(err), tx_id=c.tx_id)

    for c in sc.cmds:
        if c.hdr_words is not None and c.hdr_words < hdr_words_full():
            halt(MaxpoolError.TLAST_EARLY_HDR_IN, c)
            break
        if not c.hdr_tlast:
            halt(MaxpoolError.NO_TLAST_HDR_IN, c)
            break
        if c.cmd_type == MaxpoolCmdType.END:
            return out, status
        if c.cmd_type != MaxpoolCmdType.COMPUTE:
            halt(MaxpoolError.BAD_CMD, c)
            break
        if not c.g.valid():
            halt(MaxpoolError.BAD_PARAM, c)
            break
        resp = MaxpoolRespHdr()
        resp.tx_id = c.tx_id
        out.append(StreamBurst(resp.serialize(word_bw=WORD_BW)))
        y = maxpool_eval(c.x, c.g).ravel()
        early = c.img_words is not None and c.img_words < c.nwords_img
        if early:
            y = y[:n_emitted(c.g, min(c.img_words * PIX_PER_WORD, c.npix))]
        if y.size:
            out.append(StreamBurst(_pixels(y)))
        if early:
            halt(MaxpoolError.TLAST_EARLY_SAMP_IN, c)
            break
        if not c.img_tlast:
            halt(MaxpoolError.NO_TLAST_SAMP_IN, c)
            break
    else:
        if not sc.end:
            raise ValueError("a scenario without END must end in an error, or the kernel blocks")
    return out, status


# ---------------------------------------------------------------------------
# The scenarios
# ---------------------------------------------------------------------------

#: The three hand-computed examples (also pinned in test_maxpool_eval.py).
EX1_X = np.arange(1, 17, dtype=np.int16).reshape(4, 4)
EX1_G = Geometry(4, 4, 2, 2, 2, 2)
EX1_Y = np.array([[6, 8], [14, 16]], dtype=np.int16)

EX2_X = -np.arange(1, 10, dtype=np.int16).reshape(3, 3)
EX2_G = Geometry(3, 3, 1, 1, 2, 2)
EX2_Y = np.array([[-1, -2, 0], [-4, -5, 0], [0, 0, 0]], dtype=np.int16)

EX3_X = np.array([[3, -1, 4, -1, 5, -9],
                  [-9, 7, -6, 5, 6, -3],
                  [5, 8, -9, 7, -9, 3]], dtype=np.int16)
EX3_G = Geometry(3, 6, 2, 3, 2, 2)
EX3_Y = np.array([[7, 6]], dtype=np.int16)


def scenarios() -> dict[str, Scenario]:
    """Every scenario, from fixed seeds, in the order they run."""
    rng = np.random.default_rng(2026)

    def img(n0: int, n1: int, lo: int = -32768, hi: int = 32767) -> np.ndarray:
        return rng.integers(lo, hi, size=(max(n0, 0), max(n1, 0)), endpoint=True,
                            dtype=np.int16)

    def cmd(tx: int, n0: int, n1: int, s0: int, s1: int, k0: int, k1: int, **kw) -> Cmd:
        g = Geometry(n0, n1, s0, s1, k0, k1)
        return Cmd(tx, g, img(n0, n1) if n0 * n1 else np.zeros((0,)), **kw)

    def fresh(tx: int) -> Scenario:
        return Scenario([cmd(tx, 9, 11, 2, 2, 3, 3), cmd(tx + 1, 6, 5, 1, 1, 2, 2)])

    sc: dict[str, Scenario] = {}
    # The three hand-computed examples, as three commands in one activation.
    sc["worked_examples"] = Scenario([Cmd(1, EX1_G, EX1_X), Cmd(2, EX2_G, EX2_X),
                                      Cmd(3, EX3_G, EX3_X)])
    # Several commands with different geometry in one activation: k > s, s > k, k = s,
    # identity, maximal overlap, maximal stride.  Nothing carries over (rule 4).
    sc["geometry_mix"] = Scenario([
        cmd(11, 17, 23, 2, 2, 3, 3),     # k > s
        cmd(12, 20, 19, 5, 4, 2, 3),     # s > k: pixels skipped
        cmd(13, 16, 16, 4, 4, 4, 4),     # k = s
        cmd(14, 5, 7, 1, 1, 1, 1),       # identity
        cmd(15, 12, 13, 1, 1, 8, 8),     # maximal overlap
        cmd(16, 24, 17, 8, 8, 8, 8),     # maximal stride
        cmd(17, 13, 29, 3, 1, 5, 7),     # mixed: s0 < k0, s1 < k1, different per axis
        cmd(18, 30, 10, 1, 7, 6, 2),     # mixed the other way
    ])
    # Sizes at the limits.
    sc["limits_max"] = Scenario([
        cmd(21, 256, 256, 1, 1, 8, 8),   # largest image, largest window, every position
        cmd(22, 256, 256, 8, 8, 8, 8),
        cmd(23, 256, 256, 8, 1, 1, 8),
    ])
    sc["limits_min"] = Scenario([
        cmd(31, 1, 1, 1, 1, 1, 1),       # one pixel in, one out
        cmd(32, 1, 1, 1, 1, 8, 8),       # one pixel, window all padding but one: max(x, 0)
        cmd(33, 1, 1, 8, 8, 1, 1),       # n < s: Y is empty, no data burst
        cmd(34, 1, 256, 1, 1, 8, 8),     # one row
        cmd(35, 256, 1, 3, 1, 8, 8),     # one column
        cmd(36, 2, 2, 1, 1, 8, 8),
    ])
    # Narrow images: the same column comes round again every n1 pixels.
    sc["narrow"] = Scenario([
        cmd(41, 64, 1, 1, 1, 8, 1),
        cmd(42, 50, 2, 1, 1, 3, 2),
        cmd(43, 40, 3, 1, 2, 2, 3),
        cmd(44, 33, 4, 2, 1, 5, 4),
    ])
    # Y empty on one axis; odd pixel and output counts (the last word's high half is zero).
    sc["empty_and_odd"] = Scenario([
        cmd(51, 3, 10, 4, 1, 2, 2),      # n0 < s0
        cmd(52, 10, 5, 1, 7, 2, 2),      # n1 < s1
        cmd(53, 7, 9, 2, 2, 3, 3),       # 63 pixels in, 3 x 4 = 12 out
        cmd(54, 9, 5, 1, 2, 2, 2),       # 45 pixels in, 9 x 2 = 18 out
        cmd(55, 5, 5, 1, 1, 1, 1),       # 25 in, 25 out
    ])
    # Extreme values: all negative (the zero padding wins at the edges), the int16 limits.
    sc["extremes"] = Scenario([
        Cmd(61, Geometry(6, 7, 2, 2, 3, 3), np.full((6, 7), -32768, np.int16)),
        Cmd(62, Geometry(8, 8, 3, 3, 4, 4), img(8, 8, -32768, -1)),
        Cmd(63, Geometry(5, 6, 1, 1, 2, 2), np.where(img(5, 6, 0, 1) > 0, 32767, -32768)
            .astype(np.int16)),
    ])
    # Random geometry, random images.
    rand_cmds = []
    for t in range(8):
        n0, n1 = (int(v) for v in rng.integers(1, 41, size=2))
        s0, s1, k0, k1 = (int(v) for v in rng.integers(1, 9, size=4))
        rand_cmds.append(cmd(70 + t, n0, n1, s0, s1, k0, k1))
    sc["random_mix"] = Scenario(rand_cmds)
    # The throughput scenarios: one image each, no padded rows below the image.
    sc["timing_small"] = Scenario([cmd(81, 64, 256, 2, 2, 2, 2)], timing=True)
    sc["timing_large"] = Scenario([cmd(82, 256, 256, 2, 2, 2, 2)], timing=True)

    # One activation per error, each followed by a fresh activation that must pass.
    errs: dict[str, Scenario] = {
        "err_bad_cmd": Scenario([cmd(101, 6, 6, 2, 2, 2, 2),
                                 cmd(102, 6, 6, 2, 2, 2, 2, cmd_type=7),
                                 cmd(103, 6, 6, 2, 2, 2, 2)]),
        "err_param_n1": Scenario([cmd(111, 6, 6, 2, 2, 2, 2), cmd(112, 4, 257, 2, 2, 2, 2),
                                  cmd(113, 6, 6, 2, 2, 2, 2)]),
        "err_param_n0": Scenario([cmd(121, 0, 8, 1, 1, 1, 1), cmd(122, 6, 6, 2, 2, 2, 2)]),
        "err_param_s": Scenario([cmd(131, 6, 6, 2, 2, 2, 2), cmd(132, 8, 8, 0, 2, 2, 2)]),
        "err_param_k": Scenario([cmd(141, 8, 8, 2, 9, 2, 9), cmd(142, 6, 6, 2, 2, 2, 2)]),
        "err_hdr_early": Scenario([cmd(151, 6, 6, 2, 2, 2, 2),
                                   cmd(152, 6, 6, 2, 2, 2, 2, hdr_words=2),
                                   cmd(153, 6, 6, 2, 2, 2, 2)]),
        "err_hdr_missing": Scenario([cmd(161, 6, 6, 2, 2, 2, 2),
                                     cmd(162, 6, 6, 2, 2, 2, 2, hdr_tlast=False)]),
        "err_end_hdr_early": Scenario([cmd(171, 6, 6, 2, 2, 2, 2),
                                       Cmd(172, Geometry(0, 0, 0, 0, 0, 0),
                                           cmd_type=int(MaxpoolCmdType.END), hdr_words=1,
                                           img=False)], end=False),
        # Early TLAST on image word 14 of 150 (30 x 10 pixels, 3 x 3 windows, stride 2):
        # 30 pixels in, Y row 0 complete: 5 outputs (an odd count: the half-filled last
        # word closes the burst).
        "err_img_early": Scenario([cmd(181, 6, 6, 2, 2, 2, 2),
                                   cmd(182, 30, 10, 2, 2, 3, 3, img_words=15),
                                   cmd(183, 6, 6, 2, 2, 2, 2)]),
        # An even count of outputs (14) sent before the early TLAST: a full word closes it.
        "err_img_early_even": Scenario([cmd(191, 20, 7, 1, 2, 2, 3, img_words=20),
                                       cmd(192, 6, 6, 2, 2, 2, 2)]),
        # Early TLAST on the first image word: no output complete, no data burst.
        "err_img_early_none": Scenario([cmd(201, 10, 10, 2, 2, 2, 2, img_words=1),
                                        cmd(202, 6, 6, 2, 2, 2, 2)]),
        # The last image word has no TLAST: all of Y, then a halt.
        "err_img_missing": Scenario([cmd(211, 9, 8, 2, 2, 3, 3, img_tlast=False),
                                     cmd(212, 6, 6, 2, 2, 2, 2)]),
    }
    for k, (name, s) in enumerate(errs.items()):
        sc[name] = s
        sc[f"fresh_after_{name}"] = fresh(600 + 10 * k)
    return sc


def write_scenarios(data_dir: Path) -> list[str]:
    """Write every scenario's stimulus and expected response under ``data_dir``."""
    data_dir.mkdir(parents=True, exist_ok=True)
    names = []
    for name, sc in scenarios().items():
        d = data_dir / name
        write_bursts(stimulus(sc), d / "in")
        exp, status = expected(sc)
        write_bursts(exp, d / "expected")
        (d / "expected_status.json").write_text(json.dumps(status) + "\n", encoding="utf-8")
        meta = {"n_cmds": len(sc.cmds), "word_bw": WORD_BW, "pysim": sc.pysim,
                "timing": sc.timing,
                "npix": [c.npix for c in sc.cmds]}
        (d / "scenario.json").write_text(json.dumps(meta) + "\n", encoding="utf-8")
        names.append(name)
    (data_dir / "scenarios.txt").write_text("\n".join(names) + "\n", encoding="utf-8")
    return names


def check(data_dir: Path, stage: str, names: list[str] | None = None) -> dict[str, list[str]]:
    """Compare ``<scenario>/<stage>`` against ``<scenario>/expected``, for each scenario.

    Returns ``{scenario: [problems]}``; an empty list is a pass.  Words, burst boundaries,
    TLAST flags and the final register status must all match exactly.  For a scenario that
    ends in an error, rule 6's TLAST close gets its own message as well.
    """
    names = names or (data_dir / "scenarios.txt").read_text(encoding="utf-8").split()
    report: dict[str, list[str]] = {}
    for name in names:
        d = data_dir / name
        problems: list[str] = []
        got_dir = d / stage
        if not (got_dir / "words.bin").exists():
            report[name] = [f"no {stage} output in {got_dir}"]
            continue
        want, got = read_bursts(d / "expected"), read_bursts(got_dir)
        want_st = json.loads((d / "expected_status.json").read_text(encoding="utf-8"))
        if want_st["halted"] and got and not got[-1].tlast:
            problems.append("rule 6: the output burst in progress at the error was not "
                            "closed with TLAST")
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
        st_path = got_dir / "status.json"
        if st_path.exists():
            got_st = json.loads(st_path.read_text(encoding="utf-8"))
            if got_st != want_st:
                problems.append(f"status {got_st}, expected {want_st}")
        else:
            problems.append("no status.json")
        report[name] = problems
    return report
