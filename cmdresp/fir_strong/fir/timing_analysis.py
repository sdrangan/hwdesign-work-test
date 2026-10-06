"""timing_analysis.py -- FIR AXI4-Stream timing from a cosim VCD.

The cosim run plays every scenario of the session, so the VCD holds every command.  Each
handshake (TVALID & TREADY) on ``s_in`` is one input word, in the same order as the
concatenated stimulus files; each handshake on ``m_out`` is one output word, in the same
order as the concatenated expected responses.  The Python model (:func:`fir.fir_command`)
says how many words each command reads and writes, so every handshake is attributed to its
command and its burst (CmdHdr, payload, RespHdr, y / StatusMsg, RespFtr).

    a = analyze_session("vcd/fir_w32.vcd", "data/w32", 32)
    a.summary()                                  # the numbers results.md reports
    plot_transaction(a, "timing", 1, "out.png")  # the timing diagram of one command
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from fir import FirCmdHdr, FirRespFtr, FirState, WordReader, fir_command, words_of
from waveflow.utils.burst_io import read_bursts


@dataclass
class Cmd:
    """One command: its word ranges on each stream and their handshake cycles."""
    scenario: str
    index: int            # within the scenario
    opcode: int
    count: int
    in_hdr: tuple[int, int]      # [start, end) input word indices
    in_payload: tuple[int, int]
    out_hdr: tuple[int, int]
    out_body: tuple[int, int]    # y burst or status message (may be empty)
    out_ftr: tuple[int, int]


class Session:
    def __init__(self, bw: int, cmds: list[Cmd], t_in: np.ndarray, t_out: np.ndarray,
                 period: float, vp, sig_in: dict, sig_out: dict) -> None:
        self.bw, self.cmds, self.period = bw, cmds, period
        self.t_in, self.t_out = t_in, t_out          # handshake times, ns
        self.vp, self.sig_in, self.sig_out = vp, sig_in, sig_out

    def cyc(self, t: float) -> float:
        return t / self.period

    def find(self, scenario: str, index: int) -> Cmd:
        return next(c for c in self.cmds if c.scenario == scenario and c.index == index)

    def metrics(self, c: Cmd) -> dict:
        ti, to, p = self.t_in, self.t_out, self.period
        first_in = ti[c.in_hdr[0]]
        last_out = to[c.out_ftr[1] - 1]
        m = {"scenario": c.scenario, "index": c.index, "opcode": c.opcode, "count": c.count,
             "in_words": c.in_payload[1] - c.in_hdr[0],
             "out_words": c.out_ftr[1] - c.out_hdr[0],
             "total_cycles": int(round((last_out - first_in) / p)) + 1}
        if c.in_payload[1] > c.in_payload[0]:
            a, b = c.in_payload
            m["payload_to_footer_cycles"] = int(round((last_out - ti[a]) / p)) + 1
            n = b - a
            m["payload_words"] = n
            m["payload_cycles"] = int(round((ti[b - 1] - ti[a]) / p)) + 1
            m["input_words_per_cycle"] = n / m["payload_cycles"]
            if n > 32:  # steady state: drop 8 words at each end
                m["steady_input_words_per_cycle"] = (n - 17) / ((ti[b - 9] - ti[a + 8]) / p)
            if c.out_body[1] > c.out_body[0]:
                m["latency_first_in_to_first_out"] = int(round((to[c.out_body[0]] - ti[a]) / p))
                m["latency_last_in_to_last_out"] = int(round((to[c.out_body[1] - 1] - ti[b - 1]) / p))
        k = self.cmds.index(c)
        if k + 1 < len(self.cmds):
            nxt = ti[self.cmds[k + 1].in_hdr[0]]
            m["cycles_footer_to_next_header"] = int(round((nxt - last_out) / p))
        return m

    def summary(self) -> dict:
        proc = {}
        for c in self.cmds:
            if c.scenario == "timing" and c.opcode == 2:
                proc[str(c.count)] = self.metrics(c)
        gaps = [self.metrics(c).get("cycles_footer_to_next_header") for c in self.cmds[:-1]]
        return {"clk_period_ns": float(self.period), "process": proc,
                "n_commands": len(self.cmds),
                "cycles_footer_to_next_header": {"min": int(min(gaps)), "max": int(max(gaps))},
                "all_commands": [self.metrics(c) for c in self.cmds]}


def _commands(data_dir: Path, bw: int) -> list[Cmd]:
    """Word ranges of every command of the session, from the stimulus and the model."""
    names = (data_dir / "scenarios.txt").read_text(encoding="utf-8").split()
    hdr_words = FirCmdHdr().serialize(word_bw=bw).size
    ftr_words = FirRespFtr().serialize(word_bw=bw).size
    dt = np.uint32 if bw <= 32 else np.uint64
    cmds: list[Cmd] = []
    st, i0, o0 = FirState(), 0, 0
    for name in names:
        words = []
        for b in read_bursts(data_dir / name / "in"):
            words += words_of(b.words, b.tlast)
        rd = WordReader(words)
        k = 0
        while rd.more():
            h0 = rd.pos
            hdr = FirCmdHdr().deserialize(np.array([rd.read()[0] for _ in range(hdr_words)], dt),
                                          word_bw=bw)
            p0 = rd.pos
            out, st = fir_command(st, hdr, rd, bw)
            n = len(out)
            cmds.append(Cmd(name, k, int(hdr.opcode), int(hdr.count),
                            (i0 + h0, i0 + p0), (i0 + p0, i0 + rd.pos),
                            (o0, o0 + 1), (o0 + 1, o0 + n - ftr_words), (o0 + n - ftr_words, o0 + n)))
            o0 += n
            k += 1
        i0 += len(words)
    return cmds


def _handshake_times(bursts, period) -> np.ndarray:
    t = []
    for b in bursts:
        bt = np.asarray(b["beat_type"])
        t += list(b["tstart"] + np.nonzero(bt == 0)[0] * period)
    return np.array(sorted(t))


def analyze_session(vcd_path: str | Path, data_dir: str | Path, bw: int) -> Session:
    from vcdvcd import VCDVCD
    from waveflow.utils.vcd import VcdParser

    vp = VcdParser(VCDVCD(str(vcd_path), signals=None, store_tvs=True))
    clk = vp.add_clock_signal()
    sig_in, _ = vp.add_axiss_signals(name="s_in_T", short_name_prefix="s_in", ignore_multiple=True)
    sig_out, _ = vp.add_axiss_signals(name="m_out_T", short_name_prefix="m_out", ignore_multiple=True)
    b_in, period = vp.extract_axis_bursts(clk, sig_in)
    b_out, _ = vp.extract_axis_bursts(clk, sig_out)
    t_in, t_out = _handshake_times(b_in, period), _handshake_times(b_out, period)
    cmds = _commands(Path(data_dir), bw)
    n_in, n_out = cmds[-1].in_payload[1], cmds[-1].out_ftr[1]
    if len(t_in) != n_in or len(t_out) != n_out:
        raise ValueError(f"VCD has {len(t_in)} input / {len(t_out)} output handshakes; the "
                         f"session has {n_in} / {n_out} words")
    s = Session(bw, cmds, t_in, t_out, float(period), vp, sig_in, sig_out)
    s.clk = clk
    return s


def plot_transaction(s: Session, scenario: str, index: int, out_png: str | Path) -> None:
    """Timing diagram of one command, every burst shaded: CmdHdr, payload, RespHdr, y, RespFtr."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Patch
    from waveflow.utils.timing import TimingDiagram

    c = s.find(scenario, index)
    p = s.period
    ti, to = s.t_in, s.t_out
    t0, t1 = ti[c.in_hdr[0]] - 3 * p, to[c.out_ftr[1] - 1] + 3 * p
    td = TimingDiagram()
    td.add_signals(s.vp.get_td_signals())
    ax = td.plot_signals(add_clk_grid=False, trange=(t0, t1), text_scale_factor=1e4,
                         text_mode="never")
    colors = {"CmdHdr / RespHdr": "orange", "x payload": "green", "y": "royalblue",
              "RespFtr": "purple"}
    spans = [("s_in_TDATA", "CmdHdr / RespHdr", ti, c.in_hdr),
             ("s_in_TDATA", "x payload", ti, c.in_payload),
             ("m_out_TDATA", "CmdHdr / RespHdr", to, c.out_hdr), ("m_out_TDATA", "y", to, c.out_body),
             ("m_out_TDATA", "RespFtr", to, c.out_ftr)]
    for sig, label, t, (a, b) in spans:
        if b > a:
            # a beat handshakes at the clock edge t, so it occupies the cycle [t - p, t]
            td.add_patch(sig_name=sig, time=[t[a] - p, t[b - 1]], color=colors[label], alpha=0.35)
    m = s.metrics(c)
    ax.set_title(f"PROCESS nsamp={c.count}, D=1, ntaps=32, {s.bw}-bit words: "
                 f"latency {m['latency_first_in_to_first_out']} cycles, "
                 f"{m['total_cycles']} cycles total", fontsize=9)
    ax.set_xlabel("Time [ns]")
    ax.legend(handles=[Patch(facecolor=v, alpha=0.35, label=k) for k, v in colors.items()],
              loc="center left", bbox_to_anchor=(1.01, 0.5), fontsize=8)
    fig = ax.get_figure()
    fig.set_size_inches(14, 4.5)
    fig.savefig(out_png, dpi=130, bbox_inches="tight")
    plt.close(fig)
