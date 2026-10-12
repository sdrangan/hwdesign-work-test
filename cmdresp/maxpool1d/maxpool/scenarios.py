"""scenarios.py -- the test scenarios, their expected results, and the checker.

Each scenario is one kernel run (one ``ap_start``), written as a short list of **intents**:
"a COMPUTE command with this signal and this k", "one whose header TLAST is missing", "one
whose signal burst ends early", "then END".  From the intents, this module writes

- the stimulus, ``<data>/w32/<scenario>/in`` -- a burst bundle that the Python model, the
  pysim testbench and the C++ testbench all read; and
- the expected response, ``<data>/w32/<scenario>/expected`` plus ``expected_status.json``.

The expected response is computed from the intent, not by parsing the stimulus and not with
:func:`maxpool.maxpool_eval`: the results come from prompt.md's NumPy reference,
``x[:n // k * k].reshape(n // k, k).max(axis=1)``, so a model or a kernel that misreads the
protocol or the arithmetic cannot agree with it by sharing the mistake.

Scenarios run in the order listed: every error activation is followed by a fresh activation
that must pass (``fresh_*``).  The error scenarios keep sending commands after the bad one, as
a host with commands in flight would; the kernel must leave them unread (contract rule 6).

:func:`check` compares any run -- the pure model, pysim, csim, cosim -- against the expected
results, word for word and TLAST for TLAST.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

try:
    from examples.stream_inband.maxpool import (
        K_MAX, N_MAX, WORD_BW_SUPPORTED, Int16, MaxpoolCmdHdr, MaxpoolCmdType, MaxpoolError,
        MaxpoolRespHdr, samples_per_word,
    )
except ModuleNotFoundError:  # run from inside the project directory
    from maxpool import (  # type: ignore[no-redef]
        K_MAX, N_MAX, WORD_BW_SUPPORTED, Int16, MaxpoolCmdHdr, MaxpoolCmdType, MaxpoolError,
        MaxpoolRespHdr, samples_per_word,
    )
from waveflow.hw.arrayutils import write_array
from waveflow.utils.burst_io import StreamBurst, read_bursts, write_bursts

I16_MIN, I16_MAX = -32768, 32767


def numpy_ref(x: np.ndarray, k: int) -> np.ndarray:
    """prompt.md's reference, verbatim: ``y = x[:n // k * k].reshape(n // k, k).max(axis=1)``."""
    x = np.asarray(x, dtype=np.int16)
    n = len(x)
    return x[:n // k * k].reshape(n // k, k).max(axis=1)


@dataclass(frozen=True)
class Tx:
    """One command as intended.

    ``x`` is the signal; the header announces ``n = len(x)`` unless ``n`` is given (a
    BAD_PARAM case may announce more or fewer than it sends).  ``cmd`` is the header's
    ``cmd_type`` (COMPUTE unless given; an unknown code is an UNKNOWN_CMD case).

    Framing of the header: ``hdr_sent`` header words are sent (default: all), and
    ``hdr_tlast`` says whether the last one sent carries TLAST.  Fewer words with TLAST is an
    early header TLAST; all words without TLAST is a missing one.

    Framing of the signal: ``sent`` samples are sent (default: all ``n``), and ``tlast``
    says whether the last word sent carries TLAST.  ``sent < n`` with ``tlast`` is an early
    TLAST; it must fill whole words (``sent`` even).  ``tlast=False`` is a missing one.
    """

    tx_id: int
    x: np.ndarray
    k: int
    n: int | None = None
    cmd: int = int(MaxpoolCmdType.COMPUTE)
    hdr_sent: int | None = None
    hdr_tlast: bool = True
    sent: int | None = None
    tlast: bool = True

    @property
    def n_hdr(self) -> int:
        return len(self.x) if self.n is None else int(self.n)

    @property
    def n_sent(self) -> int:
        return len(self.x) if self.sent is None else int(self.sent)


@dataclass(frozen=True)
class Scenario:
    """One kernel run: its commands, whether END follows, and which stages run it."""

    txs: list[Tx]
    end: bool = True          # False: nothing after the last command (it must be an error)
    pysim: bool = True        # a pysim stream cannot omit TLAST or truncate a schema read
    timing: bool = False      # a throughput measurement: one long COMPUTE


def _hdr_words(cmd: int, tx_id: int, n: int, k: int, word_bw: int) -> np.ndarray:
    h = MaxpoolCmdHdr()
    h.cmd_type, h.tx_id, h.n, h.k = cmd, tx_id, n, k
    return h.serialize(word_bw=word_bw)


def _i16(x: np.ndarray, word_bw: int) -> np.ndarray:
    return write_array(np.asarray(x, dtype=np.int16), elem_type=Int16, word_bw=word_bw)


def _end(word_bw: int) -> StreamBurst:
    return StreamBurst(_hdr_words(int(MaxpoolCmdType.END), 0, 0, 0, word_bw))


def stimulus(sc: Scenario, word_bw: int) -> list[StreamBurst]:
    """The input stream: each command's header burst and signal burst, then END."""
    pf = samples_per_word(word_bw)
    bursts: list[StreamBurst] = []
    for t in sc.txs:
        hw = _hdr_words(t.cmd, t.tx_id, t.n_hdr, t.k, word_bw)
        nh = len(hw) if t.hdr_sent is None else t.hdr_sent
        bursts.append(StreamBurst(hw[:nh], t.hdr_tlast))
        if t.cmd == int(MaxpoolCmdType.END):
            continue
        if t.n_sent < len(t.x) and t.n_sent % pf:
            raise ValueError(f"tx {t.tx_id}: an early TLAST must fill whole words")
        if t.n_sent:
            bursts.append(StreamBurst(_i16(t.x[:t.n_sent], word_bw), t.tlast))
    if sc.end:
        bursts.append(_end(word_bw))
    return bursts


def expected(sc: Scenario, word_bw: int) -> tuple[list[StreamBurst], dict]:
    """The response the contract promises, from the intent alone.

    Per command, in this order of checks (the first that applies ends the run):
    a short header with TLAST is ``TLAST_EARLY_HDR``; a full header without TLAST is
    ``NO_TLAST_HDR``; a ``cmd_type`` that is neither COMPUTE nor END is ``UNKNOWN_CMD``; END
    ends the run cleanly; ``n`` or ``k`` out of range is ``BAD_PARAM``, with nothing written.
    Otherwise: the response header; then the max of every whole window among the samples the
    kernel reads, as one burst closed with TLAST (omitted if there is no whole window).  An
    early signal TLAST answers the samples sent and halts with ``TLAST_EARLY_SAMP_IN``; a
    missing one answers all ``n`` and halts with ``NO_TLAST_SAMP_IN``.  In every error the
    status ``tx_id`` is the command's ``tx_id``.
    """
    hdr_len = MaxpoolCmdHdr().serialize(word_bw=word_bw).size
    out: list[StreamBurst] = []
    ok = {"halted": 0, "error": int(MaxpoolError.NO_ERROR), "tx_id": 0}

    def halt(err: MaxpoolError, t: Tx) -> dict:
        return {"halted": 1, "error": int(err), "tx_id": t.tx_id}

    for t in sc.txs:
        if t.hdr_sent is not None and t.hdr_sent < hdr_len:
            assert t.hdr_tlast, "a short header without TLAST would block the kernel"
            return out, halt(MaxpoolError.TLAST_EARLY_HDR, t)
        if not t.hdr_tlast:
            return out, halt(MaxpoolError.NO_TLAST_HDR, t)
        if t.cmd == int(MaxpoolCmdType.END):
            return out, ok
        if t.cmd != int(MaxpoolCmdType.COMPUTE):
            return out, halt(MaxpoolError.UNKNOWN_CMD, t)
        n, k = t.n_hdr, t.k
        if not (1 <= n <= N_MAX and 1 <= k <= K_MAX and k <= n):
            return out, halt(MaxpoolError.BAD_PARAM, t)
        resp = MaxpoolRespHdr()
        resp.tx_id = t.tx_id
        out.append(StreamBurst(resp.serialize(word_bw=word_bw)))
        early = t.n_sent < n
        received = t.x[:t.n_sent] if early else t.x[:n]
        y = numpy_ref(received, k)
        if len(y):
            out.append(StreamBurst(_i16(y, word_bw)))
        if early:
            assert t.tlast, "a short signal burst without TLAST would block the kernel"
            return out, halt(MaxpoolError.TLAST_EARLY_SAMP_IN, t)
        if not t.tlast:
            return out, halt(MaxpoolError.NO_TLAST_SAMP_IN, t)
    if not sc.end:
        raise ValueError("a scenario without END must end in an error, or the kernel blocks")
    return out, ok


def scenarios() -> dict[str, Scenario]:
    """Every scenario, in the order the testbenches run them, from fixed seeds."""
    rng = np.random.default_rng(2026)

    def x(n: int, lo: int = I16_MIN, hi: int = I16_MAX) -> np.ndarray:
        return rng.integers(lo, hi, n, endpoint=True).astype(np.int16)

    def fresh(tx_id: int) -> Scenario:
        """The fresh activation after an error: two good commands, then END."""
        return Scenario([Tx(tx_id, x(13), 3), Tx(tx_id + 1, x(16), 4)])

    a16 = np.array
    sc: dict[str, Scenario] = {}
    # The three hand-computed examples (tests/test_maxpool_eval.py), in one activation.
    sc["worked"] = Scenario([
        Tx(1, a16([3, -1, 4, 1, -5, 9, 2], np.int16), 2),                 # -> [3, 4, 9]
        Tx(2, a16([-7, -3, -9, -2, -8, -1, 0, 5], np.int16), 3),          # -> [-3, -1]
        Tx(3, a16([-32768, -32768, -32768, -32768,
                   -32768, -32768, -32768, -32767], np.int16), 8),        # -> [-32767]
    ])
    # Sizes at the limits, and lengths that are not a multiple of k.
    sc["limits"] = Scenario([
        Tx(10, x(128), 8),              # n max, k max
        Tx(11, x(128), 1),              # n max, k min: 128 results
        Tx(12, x(1), 1),                # n min, k min: 1 sample, an odd word
        Tx(13, x(8), 8),                # k = n = 8
        Tx(14, x(127), 8),              # 15 windows, 7 ignored, odd n
        Tx(15, x(9), 8),                # 1 window, 1 ignored
        Tx(16, x(7), 7),                # k = n, odd
        Tx(17, x(128), 3),              # 42 windows, 2 ignored
        Tx(18, x(127), 5),              # 25 windows, 2 ignored
        Tx(19, a16([I16_MAX] * 5 + [I16_MIN] * 5, np.int16), 5),   # extremes: [32767, -32768]
        Tx(20, x(11), 2),               # 5 windows, odd number of results
    ])
    # Several COMPUTE commands with random n and k in one activation.
    txs = []
    for i in range(12):
        n = int(rng.integers(1, N_MAX, endpoint=True))
        k = int(rng.integers(1, min(n, K_MAX), endpoint=True))
        txs.append(Tx(100 + i, x(n), k))
    sc["random_multi"] = Scenario(txs)
    # One long command, for the cycle count: 128 samples at one per clock.
    sc["timing"] = Scenario([Tx(200, x(128), 8)], timing=True)

    # One activation per error, each followed by a fresh activation that must pass.
    sc["err_unknown_cmd"] = Scenario([Tx(300, x(10), 2), Tx(301, x(10), 2, cmd=7),
                                      Tx(302, x(4), 2)])
    sc["fresh_after_unknown_cmd"] = fresh(310)
    sc["err_n_zero"] = Scenario([Tx(320, x(0), 1)])
    sc["fresh_after_n_zero"] = fresh(330)
    sc["err_n_too_big"] = Scenario([Tx(340, x(6), 2), Tx(341, x(129), 1), Tx(342, x(4), 2)])
    sc["fresh_after_n_too_big"] = fresh(350)
    sc["err_k_zero"] = Scenario([Tx(360, x(10), 0)])
    sc["fresh_after_k_zero"] = fresh(370)
    sc["err_k_too_big"] = Scenario([Tx(380, x(20), 9)])
    sc["fresh_after_k_too_big"] = fresh(390)
    sc["err_k_gt_n"] = Scenario([Tx(400, x(5), 6)])
    sc["fresh_after_k_gt_n"] = fresh(410)
    sc["err_hdr_early_tlast"] = Scenario([Tx(420, x(8), 2), Tx(421, x(8), 2, hdr_sent=1),
                                          Tx(422, x(4), 2)], pysim=False)
    sc["fresh_after_hdr_early_tlast"] = fresh(430)
    sc["err_hdr_no_tlast"] = Scenario([Tx(440, x(9), 3, hdr_tlast=False)], pysim=False)
    sc["fresh_after_hdr_no_tlast"] = fresh(450)
    sc["err_end_no_tlast"] = Scenario([Tx(460, x(6), 3),
                                       Tx(0, x(0), 0, cmd=int(MaxpoolCmdType.END),
                                          hdr_tlast=False)], end=False, pysim=False)
    sc["fresh_after_end_no_tlast"] = fresh(470)
    sc["err_sig_early_tlast"] = Scenario([Tx(480, x(12), 4), Tx(481, x(20), 3, sent=10),
                                          Tx(482, x(4), 2)])
    sc["fresh_after_sig_early_tlast"] = fresh(490)
    sc["err_sig_early_tlast_no_window"] = Scenario([Tx(500, x(20), 8, sent=6)])
    sc["fresh_after_sig_early_tlast_no_window"] = fresh(510)
    sc["err_sig_no_tlast"] = Scenario([Tx(520, x(15), 4, tlast=False), Tx(521, x(4), 2)],
                                      pysim=False)
    sc["fresh_after_sig_no_tlast"] = fresh(530)
    return sc


def write_scenarios(data_dir: Path, word_bw: int) -> list[str]:
    """Write every scenario's stimulus and expected response under ``data_dir``."""
    if word_bw not in WORD_BW_SUPPORTED:
        raise ValueError(f"word_bw must be one of {WORD_BW_SUPPORTED}")
    data_dir.mkdir(parents=True, exist_ok=True)
    names = []
    for name, sc in scenarios().items():
        d = data_dir / name
        write_bursts(stimulus(sc, word_bw), d / "in")
        exp, status = expected(sc, word_bw)
        write_bursts(exp, d / "expected")
        (d / "expected_status.json").write_text(json.dumps(status) + "\n", encoding="utf-8")
        meta = {"n_cmd": len(sc.txs), "word_bw": word_bw, "samples_per_word":
                samples_per_word(word_bw), "pysim": sc.pysim, "timing": sc.timing}
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
