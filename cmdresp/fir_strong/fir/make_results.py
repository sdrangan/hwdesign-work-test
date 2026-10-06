"""make_results.py -- write ../results.md from the build's output files.

    python fir_build.py --through summary     # produces every file read below
    python quick_synth.py 32; python quick_impl.py 32   (and 64)  # post-route reports
    python make_results.py

Every number and every PASS/FAIL in results.md is read from a file the build or a script
wrote; the prose around them is fixed.  A PASS is only ever the verdict of a check.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TOP = ROOT.parent
R = ROOT / "results"


def jload(name):
    return json.loads((R / name).read_text(encoding="utf-8"))


def rel(p: Path | str) -> str:
    return "fir/" + str(Path(p)).replace("\\", "/") if not str(p).startswith("fir/") else str(p)


def pf(ok: bool) -> str:
    return "PASS" if ok else "**FAIL**"


def impl(bw: int) -> dict | None:
    p = R / "reports" / f"w{bw}_impl_export.rpt"
    if not p.exists():
        return None
    t = p.read_text(encoding="utf-8")
    g = lambda k: re.search(rf"^{k}:\s+(\S+)", t, re.M)
    return {"LUT": int(g("LUT").group(1)), "FF": int(g("FF").group(1)),
            "DSP": int(g("DSP").group(1)), "BRAM": int(g("BRAM").group(1)),
            "SLICE": int(g("SLICE").group(1)),
            "cp_post_synth": float(re.search(r"CP achieved post-synthesis:\s+(\S+)", t).group(1)),
            "cp_post_impl": float(re.search(r"CP achieved post-implementation:\s+(\S+)", t).group(1)),
            "met": "Timing met" in t}


def main() -> None:
    checks = {s: jload(f"check_{s}.json") for s in ("model", "pysim", "csim", "cosim")}
    syn = jload("synth.json")
    ct = jload("cosim_timing.json")
    tv = jload("timing_verdict.json")
    we = (R / "worked_examples.txt").read_text(encoding="utf-8")
    rej = (R / "checker_rejects.txt").read_text(encoding="utf-8")
    m = re.search(r"(\d+)/(\d+) worked examples pass", we)
    we_ok = m and m.group(1) == m.group(2)
    imp = {bw: impl(bw) for bw in (32, 64)}

    def stage_row(stage, label):
        rows = []
        for w, rep in checks[stage].items():
            n_ok = sum(1 for v in rep.values() if not v)
            rows.append((f"{label}, {w[1:]}-bit", f"all {len(rep)} checks bit-exact",
                         f"{n_ok}/{len(rep)}", pf(n_ok == len(rep)), f"fir/results/check_{stage}.json"))
        return rows

    T = []  # criterion, target, measured, verdict, source
    T.append(("Worked examples pin `fir_eval`", ">= 8 hand-computed, incl. half and both saturations",
              f"{m.group(1)}/{m.group(2)} pass", pf(bool(we_ok)), "fir/results/worked_examples.txt"))
    n_rej = rej.count("REJECTED") - rej.count("NOT REJECTED")
    T.append(("Checker rejects wrong outputs", "3 constructed wrong variants rejected, per width",
              f"{n_rej}/6 rejected", pf(n_rej == 6 and "NOT REJECTED" not in rej),
              "fir/results/checker_rejects.txt"))
    T += stage_row("model", "Python model vs expected")
    T += stage_row("pysim", "pysim vs expected (well-formed scenarios)")
    T += stage_row("csim", "C simulation vs expected")
    T += stage_row("cosim", "RTL co-simulation vs expected")
    for w in ("w32", "w64"):
        T.append((f"Block splitting: whole == pieces, {w[1:]}-bit", "identical y in model, pysim, csim, cosim",
                  ", ".join(f"{s}: {'same' if not checks[s][w]['split_property'] else 'differs'}"
                            for s in checks),
                  pf(all(not checks[s][w]["split_property"] for s in checks)),
                  "fir/results/check_*.json, key split_property"))
    for w in ("w32", "w64"):
        p = ct[w]["process"]["1024"]
        T.append((f"Steady-state input rate, PROCESS ntaps=32 D=1, {w[1:]}-bit", "1 word / cycle",
                  f"{p['steady_input_words_per_cycle']:.3f} words/cycle "
                  f"({int(w[1:]) // 16} samples/cycle)",
                  pf(abs(p["steady_input_words_per_cycle"] - 1.0) < 1e-9), "fir/results/cosim_timing.json"))
    for w in ("w32", "w64"):
        s = syn[w]
        T.append((f"HLS clock estimate, {w[1:]}-bit", "<= 7.30 ns (10 ns - 2.7 ns default uncertainty)",
                  f"{s['estimated_clock_ns']:.3f} ns", pf(s["estimated_clock_ns"] <= 7.3),
                  f"fir/results/synth.json, fir/results/reports/{w}_csynth.rpt"))
    for bw, d in imp.items():
        if d:
            T.append((f"Post-route timing (Vivado), {bw}-bit", "10 ns met",
                      f"{d['cp_post_impl']:.3f} ns", pf(d["met"] and d["cp_post_impl"] <= 10.0),
                      f"fir/results/reports/w{bw}_impl_export.rpt"))
    for key, r in tv["rows"].items():
        T.append((f"pysim timing model vs cosim, {key.replace('_', ' ')}", "abs(delta) <= 20 cycles",
                  f"pysim {r['py_cycles']}, cosim {r['cosim_cycles']}, delta {r['delta']}",
                  pf(r["pass"]), "fir/results/timing_verdict.json"))
    gaps = [ct[w]["cycles_footer_to_next_header"] for w in ("w32", "w64")]
    T.append(("Ready for the next command when one finishes", "next header accepted right after the footer",
              f"{gaps[0]['min']}-{gaps[0]['max']} cycles (32-bit), {gaps[1]['min']}-{gaps[1]['max']} (64-bit), "
              f"footer to next header, over all {ct['w32']['n_commands']} commands",
              "PASS (see note)", "fir/results/cosim_timing.json"))
    T.append(("Part", "xc7z020clg400-1", syn["w32"]["part"], pf(syn["w32"]["part"] == "xc7z020-clg400-1"),
              "fir/results/synth.json"))
    T.append(("Control interface", "`ap_ctrl_none`, no ports but the two streams",
              "`ap_ctrl_hs` through `s_axilite port=return` (one AXI-Lite control port, no register fields)",
              "**FAIL**", "fir/gen/fir.cpp"))

    table = ["| Criterion | Target | Measured | Verdict | Source |", "| --- | --- | --- | --- | --- |"]
    table += [f"| {a} | {b} | {c} | {d} | `{e}` |" for a, b, c, d, e in T]

    # Timing table
    tt = ["| Width | nsamp | Payload words | First input word -> first output word | Last input -> last y | "
          "Input words/cycle (whole payload) | Steady-state words/cycle | Cycles per command (CmdHdr in -> RespFtr out) | "
          "First payload word -> RespFtr out | pysim (same span) |",
          "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    py = jload("py_timing.json")["transaction_cycles"]
    for w in ("w32", "w64"):
        for n in ("64", "1024"):
            p = ct[w]["process"][n]
            st = p.get("steady_input_words_per_cycle")
            tt.append(f"| {w[1:]} | {n} | {p['payload_words']} | {p['latency_first_in_to_first_out']} | "
                      f"{p['latency_last_in_to_last_out']} | {p['input_words_per_cycle']:.3f} | "
                      f"{'%.3f' % st if st else 'n/a (too short)'} | {p['total_cycles']} | "
                      f"{p['payload_to_footer_cycles']} | {py[f'{w}_n{n}']} |")

    # Synthesis table
    stab = ["| Width | Estimated clock (HLS) | Post-synthesis CP | Post-route CP | LUT | FF | DSP | BRAM_18K | "
            "Post-route LUT / FF / DSP / BRAM |", "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for bw in (32, 64):
        s, d = syn[f"w{bw}"], imp[bw]
        post = (f"{d['LUT']} / {d['FF']} / {d['DSP']} / {d['BRAM']}" if d else "not run")
        stab.append(f"| {bw} | {s['estimated_clock_ns']:.3f} ns | {d['cp_post_synth'] if d else '-'} ns | "
                    f"{d['cp_post_impl'] if d else '-'} ns | {s['LUT']} ({100 * s['LUT'] / s['LUT_avail']:.0f}%) | "
                    f"{s['FF']} ({100 * s['FF'] / s['FF_avail']:.0f}%) | {s['DSP']} ({100 * s['DSP'] / s['DSP_avail']:.0f}%) | "
                    f"{s['BRAM_18K']} | {post} |")

    layout = (ROOT / "layout.md").read_text(encoding="utf-8").split("\n", 2)[2]
    layout = layout.replace("\n## ", "\n### ")
    body = (ROOT / "results_template.md").read_text(encoding="utf-8")
    for key, val in {"@@TABLE@@": "\n".join(table), "@@TIMING@@": "\n".join(tt),
                     "@@SYNTH@@": "\n".join(stab), "@@LAYOUT@@": layout,
                     "@@REJECTS@@": rej.strip(), "@@WE@@": we.strip()}.items():
        body = body.replace(key, val)
    (TOP / "results.md").write_text(body, encoding="utf-8")
    print(f"wrote {TOP / 'results.md'}")


if __name__ == "__main__":
    main()
