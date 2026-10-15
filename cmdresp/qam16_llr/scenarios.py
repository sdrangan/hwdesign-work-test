"""scenarios.py -- the test scenarios, their expected results, and the checker.

Each scenario is one kernel run (one ``ap_start``), written as a short list of **intents**:
"a DEMAP command with these samples and this inv_var", "one whose sample burst ends early",
"then END".  From the intents, this module writes

- the stimulus, ``<data>/w32/<scenario>/in`` -- a burst bundle that the Python model, the
  pysim testbench and the C++ testbench all read; and
- the expected response, ``<data>/w32/<scenario>/expected`` plus ``expected_status.json``.

The expected response is computed from the intent, not by parsing the stimulus, so it is an
independent reference: a model or a kernel that misreads the protocol cannot agree with it by
sharing the mistake.  (The arithmetic is :func:`qam16_llr.qam16_llr_eval`, which
``check_eval.py`` pins down with hand-computed examples and an independent reference.)

The error scenarios keep sending commands after the bad one, as a host with commands in flight
would.  The kernel must leave them unread (contract rule 6); the host's reset, which discards
them (rule 7), is the end of the scenario.  Each error scenario is followed by a fresh
activation (``<name>_fresh``) that must pass.

:func:`check` compares any run -- the pure model, pysim, csim, cosim -- against the expected
results, word for word and TLAST for TLAST.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from qam16_llr import (
    NSAMP_MAX, NSAMP_MIN, WORD_BW_SUPPORTED, CInt16, Int8, Qam16LlrCmdHdr, Qam16LlrCmdType,
    Qam16LlrError, Qam16LlrRespHdr, qam16_llr_eval, samples_to_array,
)
from waveflow.hw.arrayutils import write_array
from waveflow.utils.burst_io import StreamBurst, read_bursts, write_bursts

#: The scenario whose cosim latency is the timing measurement (and whose pysim run is logged).
TIMING_SCENARIO = "rand_1024"


@dataclass(frozen=True)
class Tx:
    """One command as intended.

    ``yi``/``yq`` are the samples the host has for the command (raw s8.8), demapped with
    ``inv_var`` (raw u8.8).  ``nsamp`` is what the header announces (default: the number of
    samples).  ``sent`` is how many samples the host actually sends (default: all), and
    ``tlast`` whether the last word it sends carries TLAST: ``sent < nsamp`` with ``tlast`` is
    an early TLAST; ``tlast=False`` is a missing one.  ``cmd_type`` other than DEMAP (0) or
    END (1) is a command code that names no command.
    """

    tx_id: int
    yi: np.ndarray
    yq: np.ndarray
    inv_var: int
    cmd_type: int = int(Qam16LlrCmdType.DEMAP)
    nsamp_hdr: int | None = None
    sent: int | None = None
    tlast: bool = True

    @property
    def nsamp(self) -> int:
        return int(len(self.yi)) if self.nsamp_hdr is None else int(self.nsamp_hdr)

    @property
    def n_sent(self) -> int:
        return int(len(self.yi)) if self.sent is None else int(self.sent)


@dataclass(frozen=True)
class Scenario:
    """One kernel run: its commands, whether END follows, and which stages run it."""

    txs: list[Tx]
    end: bool = True          # False: nothing after the last command (it must be an error)
    pysim: bool = True        # a pysim stream cannot omit TLAST
    desc: str = ""


def _hdr(cmd: int, word_bw: int, tx_id: int = 0, nsamp: int = 0, inv_var: int = 0) -> np.ndarray:
    h = Qam16LlrCmdHdr()
    h.cmd_type, h.tx_id, h.nsamp, h.inv_var = cmd, tx_id, nsamp, inv_var
    return h.serialize(word_bw=word_bw)


def _samples(yi: np.ndarray, yq: np.ndarray, word_bw: int) -> np.ndarray:
    return write_array(samples_to_array(yi, yq), elem_type=CInt16, word_bw=word_bw)


def _llrs(inv_var: int, yi: np.ndarray, yq: np.ndarray, word_bw: int) -> np.ndarray:
    llr = qam16_llr_eval(inv_var, yi, yq).reshape(-1).astype(np.int8)
    return write_array(llr, elem_type=Int8, word_bw=word_bw)


def stimulus(sc: Scenario, word_bw: int) -> list[StreamBurst]:
    """The input stream: each command's header and the samples the host sends, then END."""
    bursts: list[StreamBurst] = []
    for t in sc.txs:
        bursts.append(StreamBurst(_hdr(t.cmd_type, word_bw, t.tx_id, t.nsamp, t.inv_var)))
        if t.n_sent:
            bursts.append(StreamBurst(_samples(t.yi[:t.n_sent], t.yq[:t.n_sent], word_bw), t.tlast))
    if sc.end:
        bursts.append(StreamBurst(_hdr(int(Qam16LlrCmdType.END), word_bw)))
    return bursts


def expected(sc: Scenario, word_bw: int) -> tuple[list[StreamBurst], dict]:
    """The response the contract and prompt.md promise, from the intent alone.

    Per command, in stream order (the first error wins): a code that is not DEMAP halts with
    ``UNKNOWN_CMD``; ``nsamp`` outside 1..1024 halts with ``BAD_PARAM``; neither writes
    anything for the command.  Otherwise the response header, then one LLR word per sample the
    kernel reads, as one burst closed with TLAST -- also when the input burst ended early
    (rule 6).  An early TLAST answers the samples sent and halts with ``TLAST_EARLY_SAMP_IN``;
    a missing TLAST answers all ``nsamp`` and halts with ``NO_TLAST_SAMP_IN``.  Nothing after a
    halt is answered.  The status starts clear.
    """
    out: list[StreamBurst] = []
    status = {"halted": 0, "error": int(Qam16LlrError.NO_ERROR), "tx_id": 0}
    for t in sc.txs:
        err = Qam16LlrError.NO_ERROR
        if t.cmd_type != Qam16LlrCmdType.DEMAP:
            err = Qam16LlrError.UNKNOWN_CMD
        elif not NSAMP_MIN <= t.nsamp <= NSAMP_MAX:
            err = Qam16LlrError.BAD_PARAM
        else:
            resp = Qam16LlrRespHdr()
            resp.tx_id = t.tx_id
            out.append(StreamBurst(resp.serialize(word_bw=word_bw)))
            early = t.n_sent < t.nsamp
            n_out = t.n_sent if early else t.nsamp
            out.append(StreamBurst(_llrs(t.inv_var, t.yi[:n_out], t.yq[:n_out], word_bw)))
            if early and t.tlast:
                err = Qam16LlrError.TLAST_EARLY_SAMP_IN
            elif not t.tlast:
                err = Qam16LlrError.NO_TLAST_SAMP_IN
        if err != Qam16LlrError.NO_ERROR:
            status = {"halted": 1, "error": int(err), "tx_id": t.tx_id}
            break
    else:
        if not sc.end:
            raise ValueError("a scenario without END must end in an error, or the kernel blocks")
    return out, status


def scenarios() -> dict[str, Scenario]:
    """Every scenario, from a fixed seed, in the order the testbenches run them."""
    rng = np.random.default_rng(2026)

    def qam(n: int) -> tuple[np.ndarray, np.ndarray]:
        """Noisy 16-QAM points: levels -3, -1, 1, 3 (x256, s8.8) plus Gaussian noise."""
        lv = np.array([-3, -1, 1, 3])
        yi = rng.choice(lv, n) * 256 + rng.normal(0, 90, n)
        yq = rng.choice(lv, n) * 256 + rng.normal(0, 90, n)
        return (np.clip(np.round(yi), -32768, 32767).astype(np.int64),
                np.clip(np.round(yq), -32768, 32767).astype(np.int64))

    def full(n: int) -> tuple[np.ndarray, np.ndarray]:
        """Uniform over the whole int16 range."""
        return (rng.integers(-32768, 32768, n, dtype=np.int64),
                rng.integers(-32768, 32768, n, dtype=np.int64))

    def tx(tx_id: int, s: tuple[np.ndarray, np.ndarray], inv_var: int, **kw) -> Tx:
        return Tx(tx_id, s[0], s[1], inv_var, **kw)

    # nsamp = 1024: 960 noisy constellation points and 64 full-range samples, shuffled.
    qi, qq = qam(960)
    fi, fq = full(64)
    perm = rng.permutation(1024)
    r1024 = (np.concatenate([qi, fi])[perm], np.concatenate([qq, fq])[perm])
    inv_1024 = int(rng.integers(0x0080, 0x0400))
    # Full scale on both axes, every sign combination: with inv_var = 0xFFFF every LLR saturates.
    sat = (np.array([32767, -32768, 32767, -32768, 32767, -32767]),
           np.array([32767, -32768, -32768, 32767, -32767, 32767]))

    return {
        "one_samp":   Scenario([tx(1, qam(1), 0x0100)], desc="nsamp = 1"),
        TIMING_SCENARIO: Scenario([tx(2, r1024, inv_1024)],
                                  desc=f"nsamp = 1024, random samples, inv_var = {inv_1024:#06x}"),
        "saturate":   Scenario([tx(3, sat, 0xFFFF)], desc="full-scale samples, inv_var = 0xFFFF"),
        "two_cmds":   Scenario([tx(4, qam(37), 0x0040), tx(5, qam(300), 0x0A80)],
                               desc="two DEMAP, nsamp 37 / 300, inv_var 0x0040 / 0x0A80"),
        # An unknown command code (7) after a good command; its burst and END stay unread.
        "unknown_cmd": Scenario([tx(50, qam(3), 0x0100), tx(51, qam(4), 0x0100, cmd_type=7),
                                 tx(52, qam(2), 0x0100)],
                                desc="DEMAP, then cmd_type = 7, then DEMAP queued"),
        "unknown_cmd_fresh": Scenario([tx(53, qam(8), 0x0000)],
                                      desc="fresh activation, inv_var = 0 (all LLRs 0)"),
        # nsamp = 1025: nothing is sent for it, and its full sample burst is never read.
        "bad_nsamp":  Scenario([tx(60, qam(2), 0x0200), tx(61, qam(1025), 0x0200),
                                tx(62, qam(2), 0x0200)],
                               desc="DEMAP, then nsamp = 1025 with its burst, then DEMAP queued"),
        "bad_nsamp_fresh": Scenario([tx(63, qam(5), 0x0300)], desc="fresh activation"),
        # The burst ends after 6 of 10 samples: 6 LLR words, closed with TLAST, then a halt.
        "early_tlast": Scenario([tx(70, qam(4), 0x0100), tx(71, qam(10), 0x0180, sent=6),
                                 tx(72, qam(3), 0x0100)],
                                desc="DEMAP, then 10 samples announced and 6 sent, then DEMAP queued"),
        "early_tlast_fresh": Scenario([tx(73, qam(7), 0x0180)], desc="fresh activation"),
        # The last sample word has no TLAST: all 5 LLR words, then a halt.
        "no_tlast":   Scenario([tx(80, qam(5), 0x0100, tlast=False), tx(81, qam(2), 0x0100)],
                               pysim=False, desc="5 samples, no TLAST, then DEMAP queued"),
        "no_tlast_fresh": Scenario([tx(83, qam(9), 0x0200), tx(84, qam(1), 0x0050)],
                                   desc="fresh activation, two DEMAP"),
    }


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
        meta = {"n_tx": len(sc.txs), "word_bw": word_bw, "pysim": sc.pysim, "desc": sc.desc}
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
