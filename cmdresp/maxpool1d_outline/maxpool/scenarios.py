"""scenarios.py -- the test scenarios, their expected results, and the checker.

Each scenario is one kernel run (one ``ap_start``), written as a short list of **intents**:
"a COMPUTE with these samples and this window", "one whose signal ends early", "one whose
header has no TLAST", "then END".  From the intents, this module writes

- the stimulus, ``<data>/w32/<scenario>/in`` -- a burst bundle that the Python model, the
  pysim testbench and the C++ testbench all read; and
- the expected response, ``<data>/w32/<scenario>/expected`` plus ``expected_status.json``.

The expected response is computed from the intent, not by parsing the stimulus, so it is an
independent reference.  (The arithmetic is :func:`maxpool.maxpool_eval`, pinned down by the
hand-computed examples and the NumPy reference in ``test_eval.py``.)

Every error scenario is followed by a ``<name>__fresh`` scenario: a new activation, on fresh
streams, that must pass.  Scenarios whose name ends in ``_q`` queue further commands behind
the failing one, which the kernel must leave unread; they run in the model and csim only,
because a co-simulated stream must be consumed in full (cosim replays every word the C
testbench wrote).  ``scenarios_cosim.txt`` lists the rest, in order: the one co-simulation
walks through all of them.

:func:`check` compares any run -- the model, pysim, csim, cosim -- against the expected
results, word for word and TLAST for TLAST.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from maxpool import (
    WORD_BW, Int16, MaxpoolCmdHdr, MaxpoolCmdType, MaxpoolError, MaxpoolRespHdr,
    maxpool_eval, samples_per_word,
)
from waveflow.hw.arrayutils import write_array
from waveflow.utils.burst_io import StreamBurst, read_bursts, write_bursts

COMPUTE = int(MaxpoolCmdType.COMPUTE)
END = int(MaxpoolCmdType.END)


def _tx_id_word() -> int:
    """Which header word carries tx_id: found by serializing, not by reading the schema."""
    h = MaxpoolCmdHdr()
    h.tx_id = 0xFFFF
    return int(np.flatnonzero(h.serialize(word_bw=WORD_BW))[0])


TX_ID_WORD = _tx_id_word()


@dataclass(frozen=True)
class Tx:
    """One command as intended.

    ``samples`` is the signal; ``n`` defaults to its length (override it for BAD_PARAM).
    ``sent`` is how many samples the host sends (default: all ``n``, when the header is
    accepted; none otherwise) -- ``sent < n`` with ``tlast`` is an early TLAST on the signal,
    ``tlast=False`` a missing one; an early TLAST ends on a word boundary, so ``sent`` is even.
    ``cmd`` is the raw command code.  ``hdr`` is ``"ok"``, ``"early"`` (only the first header
    word is sent, with TLAST) or ``"missing"`` (both words, no TLAST).
    """

    tx_id: int
    samples: np.ndarray = field(default_factory=lambda: np.zeros(0, np.int16))
    k: int = 1
    n: int | None = None
    cmd: int = COMPUTE
    hdr: str = "ok"
    sent: int | None = None
    tlast: bool = True

    @property
    def nsamp(self) -> int:
        return int(len(self.samples)) if self.n is None else int(self.n)

    def header_error(self) -> MaxpoolError:
        """The error the header raises, in the spec's order: TLAST, command, parameters."""
        if self.hdr == "early":
            return MaxpoolError.TLAST_EARLY_HDR
        if self.hdr == "missing":
            return MaxpoolError.NO_TLAST_HDR
        if self.cmd not in (COMPUTE, END):
            return MaxpoolError.UNKNOWN_CMD
        if self.cmd == COMPUTE and not (1 <= self.nsamp <= 128 and 1 <= self.k <= min(self.nsamp, 8)):
            return MaxpoolError.BAD_PARAM
        return MaxpoolError.NO_ERROR

    @property
    def n_sent(self) -> int:
        if self.sent is not None:
            return int(self.sent)
        return self.nsamp if self.header_error() == MaxpoolError.NO_ERROR else 0


def End(**kw) -> Tx:  # noqa: N802 -- an END command as an intent
    return Tx(tx_id=0, cmd=END, **kw)


@dataclass(frozen=True)
class Scenario:
    """One kernel run: its commands, in order.  END is appended unless ``end`` is False."""

    txs: list[Tx]
    end: bool = True
    queued: list[Tx] = field(default_factory=list)   # sent after an error; must stay unread

    @property
    def cosim(self) -> bool:
        return not self.queued

    @property
    def pysim(self) -> bool:
        return expected_status(self)["halted"] == 0


def _hdr_words(t: Tx) -> np.ndarray:
    h = MaxpoolCmdHdr()
    h.cmd_type, h.tx_id = t.cmd, t.tx_id
    h.n, h.k = (t.nsamp, t.k) if t.cmd != END else (0, 0)
    return h.serialize(word_bw=WORD_BW)


def _int16(x: np.ndarray) -> np.ndarray:
    return write_array(np.asarray(x, dtype=np.int16), elem_type=Int16, word_bw=WORD_BW)


def _tx_bursts(t: Tx) -> list[StreamBurst]:
    w = _hdr_words(t)
    if t.hdr == "early":
        out = [StreamBurst(w[:1], True)]
    else:
        out = [StreamBurst(w, t.hdr != "missing")]
    if t.n_sent:
        out.append(StreamBurst(_int16(t.samples[:t.n_sent]), t.tlast))
    return out


def stimulus(sc: Scenario) -> list[StreamBurst]:
    """The input stream: each command's header and samples, then END, then anything queued."""
    bursts: list[StreamBurst] = []
    for t in sc.txs:
        if t.n_sent < t.nsamp and t.n_sent % 2:
            raise ValueError(f"tx {t.tx_id}: an early TLAST must fill whole words (sent even)")
        bursts += _tx_bursts(t)
    if sc.end:
        bursts += _tx_bursts(End())
    for t in sc.queued:
        bursts += _tx_bursts(t)
    return bursts


def expected(sc: Scenario) -> tuple[list[StreamBurst], dict]:
    """The response the spec promises, from the intent alone.

    A header error writes nothing for its command, and its status ``tx_id`` is the command's
    ``tx_id`` unless the header ended before the word that carries it (then 0).  An accepted
    COMPUTE writes the response header, then ``y`` for every whole window among the samples
    the kernel received (an early TLAST: the samples sent; otherwise all ``n``), as one burst
    closed with TLAST -- no burst if no window is whole.  Nothing after an error is answered.
    """
    out: list[StreamBurst] = []
    status = {"halted": 0, "error": int(MaxpoolError.NO_ERROR), "tx_id": 0}
    txs = list(sc.txs) + ([End()] if sc.end else [])
    for t in txs:
        herr = t.header_error()
        if herr != MaxpoolError.NO_ERROR:
            got_tx_id = t.hdr != "early" or TX_ID_WORD == 0
            status = {"halted": 1, "error": int(herr), "tx_id": t.tx_id if got_tx_id else 0}
            break
        if t.cmd == END:
            break
        resp = MaxpoolRespHdr()
        resp.tx_id = t.tx_id
        out.append(StreamBurst(resp.serialize(word_bw=WORD_BW)))
        early = t.n_sent < t.nsamp
        received = t.n_sent if early else t.nsamp
        y = maxpool_eval(t.samples[:received], t.k)
        if y.size:
            out.append(StreamBurst(_int16(y)))
        err = (MaxpoolError.TLAST_EARLY_SAMP_IN if early
               else MaxpoolError.NO_TLAST_SAMP_IN if not t.tlast
               else MaxpoolError.NO_ERROR)
        if err != MaxpoolError.NO_ERROR:
            status = {"halted": 1, "error": int(err), "tx_id": t.tx_id}
            break
    else:
        raise ValueError("a scenario without END must end in an error, or the kernel blocks")
    return out, status


def expected_status(sc: Scenario) -> dict:
    return expected(sc)[1]


def scenarios() -> dict[str, Scenario]:
    """Every scenario, from fixed seeds, in the order the testbenches run them."""
    rng = np.random.default_rng(2026)

    def x(n: int) -> np.ndarray:
        return rng.integers(-32768, 32768, n).astype(np.int16)

    def fresh(seed_n: int) -> Scenario:
        return Scenario([Tx(900 + seed_n, x(12), k=3), Tx(901 + seed_n, x(9), k=2)])

    edge = np.array([-32768, 32767, -1, 0, -32768, -32768, 32767, 32767], dtype=np.int16)
    good: dict[str, Scenario] = {
        # Sizes at the limits.
        "lim_n1_k1":    Scenario([Tx(1, x(1), k=1)]),
        "lim_n128_k8":  Scenario([Tx(2, x(128), k=8)]),
        "lim_n128_k1":  Scenario([Tx(3, x(128), k=1)]),
        "lim_n8_k8":    Scenario([Tx(4, x(8), k=8)]),
        "lim_n2_k2":    Scenario([Tx(5, x(2), k=2)]),
        "lim_txid":     Scenario([Tx(0xFFFF, x(4), k=2), Tx(0, x(3), k=1)]),
        # Extreme values: the int16 range, ties, all-negative windows.
        "values_edge":  Scenario([Tx(6, edge, k=2), Tx(7, edge, k=4), Tx(8, -np.abs(x(16)) - 1, k=5)]),
        # Signal lengths that are not a multiple of k (and odd n: a half-filled last word).
        "rem_n10_k3":   Scenario([Tx(10, x(10), k=3)]),
        "rem_n127_k8":  Scenario([Tx(11, x(127), k=8)]),
        "rem_n7_k6":    Scenario([Tx(12, x(7), k=6)]),
        "rem_n100_k7":  Scenario([Tx(13, x(100), k=7)]),
        # Several COMPUTE commands with different n and k in one activation.
        "multi_cmd":    Scenario([Tx(20, x(17), k=4), Tx(21, x(1), k=1), Tx(22, x(64), k=5),
                                  Tx(23, x(33), k=2), Tx(24, x(8), k=8), Tx(25, x(9), k=3)]),
        # END alone: an activation with no work.
        "end_only":     Scenario([]),
        # Timing: the longest activation, and two single commands whose difference in
        # cycles is the cycles per input word.
        "timing":       Scenario([Tx(30, x(128), k=1), Tx(31, x(128), k=8), Tx(32, x(128), k=5)]),
        "tput_n64":     Scenario([Tx(33, x(64), k=1)]),
        "tput_n128":    Scenario([Tx(34, x(128), k=1)]),
    }
    errors: dict[str, Scenario] = {
        # Header errors (one per code), each after a good command, so its response is closed.
        "err_unknown_cmd":   Scenario([Tx(40, x(6), k=2), Tx(41, x(6), k=2, cmd=5)], end=False),
        "err_bad_n0":        Scenario([Tx(42, x(0), k=1)], end=False),
        "err_bad_n129":      Scenario([Tx(43, x(129), k=1)], end=False),
        "err_bad_k0":        Scenario([Tx(44, x(10), k=0)], end=False),
        "err_bad_k9":        Scenario([Tx(45, x(100), k=9)], end=False),
        "err_bad_k_gt_n":    Scenario([Tx(46, x(4), k=3), Tx(47, x(5), k=6)], end=False),
        "err_hdr_early":     Scenario([Tx(48, x(6), k=3), Tx(49, x(6), k=3, hdr="early")], end=False),
        "err_hdr_missing":   Scenario([Tx(50, x(6), k=3), Tx(51, x(6), k=3, hdr="missing")], end=False),
        "err_end_missing":   Scenario([Tx(52, x(4), k=4), End(hdr="missing")], end=False),
        # Precedence within one header: TLAST, then command, then parameters.
        "err_prec_early_cmd":   Scenario([Tx(53, x(0), k=0, cmd=9, hdr="early")], end=False),
        "err_prec_missing_cmd": Scenario([Tx(54, x(0), k=0, cmd=9, hdr="missing")], end=False),
        "err_prec_cmd_param":   Scenario([Tx(55, x(0), k=0, cmd=2)], end=False),
        # Signal errors.
        "err_sig_early":      Scenario([Tx(60, x(20), k=3, sent=8)], end=False),       # 2 windows
        "err_sig_early_held": Scenario([Tx(61, x(16), k=4, sent=10)], end=False),      # 2 windows, both before the TLAST word
        "err_sig_early_none": Scenario([Tx(62, x(9), k=3), Tx(63, x(20), k=8, sent=6)], end=False),  # no window
        "err_sig_early_k1":   Scenario([Tx(64, x(10), k=1, sent=2)], end=False),
        "err_sig_missing":    Scenario([Tx(65, x(11), k=2, tlast=False)], end=False),
        "err_sig_missing_one": Scenario([Tx(66, x(1), k=1, tlast=False)], end=False),
        # As above, with commands queued behind the failing one (model and csim only).
        "err_unknown_cmd_q":  Scenario([Tx(70, x(6), k=2, cmd=3)], end=False,
                                       queued=[Tx(71, x(6), k=2), End()]),
        "err_bad_param_q":    Scenario([Tx(72, x(10), k=9, sent=10)], end=False,
                                       queued=[Tx(73, x(6), k=2), End()]),
        "err_hdr_early_q":    Scenario([Tx(74, x(6), k=3, hdr="early")], end=False,
                                       queued=[Tx(75, x(6), k=2), End()]),
        "err_sig_early_q":    Scenario([Tx(76, x(20), k=4, sent=8)], end=False,
                                       queued=[Tx(77, x(6), k=2), End()]),
        "err_sig_missing_q":  Scenario([Tx(78, x(12), k=5, tlast=False)], end=False,
                                       queued=[Tx(79, x(6), k=2), End()]),
    }
    out = dict(good)
    for i, (name, sc) in enumerate(errors.items()):
        out[name] = sc
        out[f"{name}__fresh"] = fresh(2 * i)
    return out


def write_scenarios(data_dir: Path) -> list[str]:
    """Write every scenario's stimulus and expected response under ``data_dir``."""
    data_dir.mkdir(parents=True, exist_ok=True)
    names, cosim_names = [], []
    for name, sc in scenarios().items():
        d = data_dir / name
        write_bursts(stimulus(sc), d / "in")
        exp, status = expected(sc)
        write_bursts(exp, d / "expected")
        (d / "expected_status.json").write_text(json.dumps(status) + "\n", encoding="utf-8")
        meta = {"n_tx": len(sc.txs), "word_bw": WORD_BW, "samples_per_word": samples_per_word(),
                "pysim": sc.pysim, "cosim": sc.cosim}
        (d / "scenario.json").write_text(json.dumps(meta) + "\n", encoding="utf-8")
        names.append(name)
        if sc.cosim:
            cosim_names.append(name)
    (data_dir / "scenarios.txt").write_text("\n".join(names) + "\n", encoding="utf-8")
    (data_dir / "scenarios_cosim.txt").write_text("\n".join(cosim_names) + "\n", encoding="utf-8")
    return names


def check(data_dir: Path, stage: str, names: list[str] | None = None) -> dict[str, list[str]]:
    """Compare ``<scenario>/<stage>`` against ``<scenario>/expected``, for each scenario.

    Returns ``{scenario: [problems]}``; an empty list is a pass.  Words, burst boundaries,
    TLAST flags and the final register status must all match exactly.
    """
    names = names or (data_dir / "scenarios.txt").read_text(encoding="utf-8").split()
    report: dict[str, list[str]] = {}
    for name in names:
        d = data_dir / name
        problems: list[str] = []
        got_dir = d / stage
        if not (got_dir / "words.bin").exists() and not (got_dir / "status.json").exists():
            report[name] = [f"no {stage} output in {got_dir}"]
            continue
        want = read_bursts(d / "expected")
        got = read_bursts(got_dir) if (got_dir / "words.bin").exists() else []
        want_st = json.loads((d / "expected_status.json").read_text(encoding="utf-8"))
        if want_st["halted"] and got and not got[-1].tlast:
            problems.append("the output burst in progress at the error was not closed with TLAST")
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
